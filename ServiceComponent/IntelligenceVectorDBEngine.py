import datetime
import math
import time
import logging
import threading
from typing import Optional, Tuple, List, Dict, Any

from VectorDB.VectorDBClient import RemoteCollection
from ServiceComponent.IntelligenceHubDefines_v2 import (
    ArchivedData,
    APPENDIX_TOTAL_SCORE,
    APPENDIX_TIME_ARCHIVED,
    APPENDIX_TIME_PUB,  # Added for v2 compatibility
)

logger = logging.getLogger(__name__)


class IntelligenceVectorDBEngine:
    """
    Business logic wrapper for VectorDB, compatible with both v1 and v2 schemas.
    """

    def __init__(self, vector_db_collection: RemoteCollection, batch_size: int = 50,
                 query_cache_size: int = 256, query_cache_ttl: float = 300.0):
        self.collection = vector_db_collection
        self.batch_size = batch_size
        self._buffer: List[Dict] = []
        self._buffer_lock = threading.RLock()
        # 查询缓存从无 TTL 的 lru_cache 改为带 TTL 的手工 LRU:
        # 服务端(VectorStorageEngine.Repo)已改实例级缓存,这里同样加 TTL,
        # 避免查询结果永不过期导致数据更新后检索不到。
        self._query_cache: Dict[Any, Tuple[float, List[Dict]]] = {}
        self._query_cache_lock = threading.Lock()
        self._query_cache_maxsize = query_cache_size
        self._query_cache_ttl = query_cache_ttl

    def _parse_timestamp_safe(self, time_val: Any) -> Optional[float]:
        if time_val is None:
            return None
        if isinstance(time_val, (int, float)):
            return float(time_val)
        if isinstance(time_val, datetime.datetime):
            return time_val.timestamp()
        if isinstance(time_val, str):
            if not time_val.strip():
                return None
            try:
                return datetime.datetime.fromisoformat(time_val.replace('Z', '+00:00')).timestamp()
            except ValueError:
                return None
        return None

    def _prepare_document(self, intelligence: ArchivedData, data_type: str) -> Optional[Dict]:
        """
        Transforms ArchivedData to VectorDB dict with v1/v2 compatibility.
        """
        # 1. Text Construction
        if data_type == 'summary':
            text_parts = [
                intelligence.EVENT_TITLE,
                intelligence.EVENT_BRIEF,
                intelligence.EVENT_TEXT
            ]
            full_text = "\n\n".join([str(t) for t in text_parts if t and str(t).strip()])
        else:
            # Compatibility: v2 uses RAW_DATA['content'], v1 might have content elsewhere or raw
            raw = intelligence.RAW_DATA or {}
            full_text = raw.get('content', '') or getattr(intelligence, 'content', '')

        if not full_text:
            logger.warning(f"Empty text for UUID {intelligence.UUID}, skipping vectorization.")
            return None

        # 2. Advanced Time Compatibility (v1 vs v2)
        appendix = intelligence.APPENDIX or {}

        # Determine Publish Time (v1: root.PUB_TIME | v2: appendix.__TIME_PUB__)
        raw_pub_time = getattr(intelligence, 'PUB_TIME', None)  # Try v1 root field
        if raw_pub_time is None:
            raw_pub_time = appendix.get(APPENDIX_TIME_PUB)  # Try v2 appendix field

        pub_ts = self._parse_timestamp_safe(raw_pub_time)

        # Determine Archive Time
        raw_archived_time = appendix.get(APPENDIX_TIME_ARCHIVED)
        archived_ts = self._parse_timestamp_safe(raw_archived_time) or datetime.datetime.now().timestamp()

        # 3. Score Compatibility (v1: MAX_RATE_SCORE | v2: TOTAL_SCORE)
        # We try v2 key first, then fallback to v1 specific key if present in appendix
        total_score = appendix.get(APPENDIX_TOTAL_SCORE)
        if total_score is None:
            total_score = appendix.get('__MAX_RATE_SCORE__', 0.0)  # Fallback to v1 string key

        # P8 修复:total_score 可能是非数值字符串或 NaN,直接 float() 会抛异常或
        # 把 NaN 写入 ChromaDB(导致后续排序/比较异常)。统一安全转换,NaN/inf/异常→0.0。
        def _safe_score(v) -> float:
            try:
                f = float(v)
            except (TypeError, ValueError):
                return 0.0
            if math.isnan(f) or math.isinf(f):
                return 0.0
            return f
        safe_total_score = _safe_score(total_score)

        # 4. Metadata Construction
        metadata = {
            "uuid": intelligence.UUID,
            "informant": intelligence.INFORMANT,
            "archived_timestamp": archived_ts,
            "total_score": safe_total_score,
            # 'timestamp' is the primary key for temporal analysis in the DB engine
            "timestamp": pub_ts if pub_ts is not None else archived_ts
        }

        if pub_ts is not None:
            metadata["pub_timestamp"] = pub_ts

        # Optional: v1 Rate Class compatibility
        rate_class = appendix.get('__MAX_RATE_CLASS__')
        if rate_class:
            metadata["max_rate_class"] = str(rate_class)

        return {
            "doc_id": intelligence.UUID,
            "text": full_text,
            "metadata": metadata
        }

    def upsert(self, intelligence: ArchivedData, data_type: str, timeout: float = 120):
        doc = self._prepare_document(intelligence, data_type)
        if doc:
            self.collection.upsert(**doc, timeout=timeout)

    def add_to_batch(self, intelligence: ArchivedData, data_type: str, timeout: float = 120):
        doc = self._prepare_document(intelligence, data_type)
        if not doc:
            return
        with self._buffer_lock:
            self._buffer.append(doc)
            buffer_len = len(self._buffer)
        if buffer_len >= self.batch_size:
            self.commit(timeout)

    def commit(self, timeout: float = 120):
        with self._buffer_lock:
            if not self._buffer:
                return
            batch_to_send = list(self._buffer)
        try:
            self.collection.upsert_batch(batch_to_send, timeout=timeout)
        except Exception as e:
            # 失败时保留 buffer,等待下一次 commit 重试,
            # 避免 finally: clear() 使失败后的整批数据丢失。
            logger.error(f"Error committing batch: {e}")
            return
        with self._buffer_lock:
            # 只移除已成功发送的项(期间可能又有新项加入)
            for doc in batch_to_send:
                try:
                    self._buffer.remove(doc)
                except ValueError:
                    # 并发下其他线程已移除同一项：list.remove 的 ValueError 属预期竞态
                    pass

    def _raw_query(self,
                   text: str,
                   top_n: int = 5,
                   score_threshold: float = 0.0,
                   event_period: Optional[Tuple[datetime.datetime, datetime.datetime]] = None,
                   archive_period: Optional[Tuple[datetime.datetime, datetime.datetime]] = None,
                   rate_class: Optional[str] = None,
                   rate_threshold: Optional[float] = None,
                   timeout: int = 30,
                   force_db_filter: bool = False,
                   post_filter_multiplier: int = 10,
                   ) -> List[Dict]:
        """
        Internal query implementation. Use self.query() to take advantage of LRU cache.
        """
        filters = []

        # NOTE: The timestamp and pub_timestamp fields are int type. But archived_timestamp field is float.
        if event_period:
            filters.append({
                "timestamp": {"$gte": int(event_period[0].timestamp())}
            })
            filters.append({
                "timestamp": {"$lte": int(event_period[1].timestamp())}
            })

        if archive_period:
            filters.append({"archived_timestamp": {"$gte": archive_period[0].timestamp()}})
            filters.append({"archived_timestamp": {"$lte": archive_period[1].timestamp()}})

        if rate_class:
            filters.append({"max_rate_class": rate_class})

        if rate_threshold is not None:
            # Query against total_score (v2) or fallback logic in DB
            filters.append({"total_score": {"$gte": rate_threshold}})

        where_clause = None
        if len(filters) == 1:
            where_clause = filters[0]
        elif len(filters) > 1:
            where_clause = {"$and": filters}

        return self.collection.search(
            query=text,
            top_n=top_n,
            score_threshold=score_threshold,
            filter_criteria=where_clause,
            timeout=timeout,
            force_db_filter=force_db_filter,
            post_filter_multiplier=post_filter_multiplier,
        )

    def query(self,
              text: str,
              top_n: int = 5,
              score_threshold: float = 0.0,
              event_period: Optional[Tuple[datetime.datetime, datetime.datetime]] = None,
              archive_period: Optional[Tuple[datetime.datetime, datetime.datetime]] = None,
              rate_class: Optional[str] = None,
              rate_threshold: Optional[float] = None,
              timeout: int = 30,
              force_db_filter: bool = False,
              post_filter_multiplier: int = 10,
              ) -> List[Dict]:
        """
        Query with support for both v1 and v2 metadata fields.
        Caches results by (text, top_n, score_threshold, periods, rate filters,
        force_db_filter, post_filter_multiplier).
        """
        # 手动 TTL LRU:key 由全部查询参数构成;过期或缺失则执行真实查询并缓存。
        cache_key = (
            text,
            top_n,
            score_threshold,
            event_period,
            archive_period,
            rate_class,
            rate_threshold,
            # 注意:timeout 不参与缓存 key(只是单次调用预算)
            force_db_filter,
            post_filter_multiplier,
        )
        now = time.time()
        with self._query_cache_lock:
            entry = self._query_cache.get(cache_key)
            if entry is not None and (now - entry[0]) < self._query_cache_ttl:
                return entry[1]

        # 未命中或已过期:真实查询
        result = self._raw_query(
            text,
            top_n,
            score_threshold,
            event_period,
            archive_period,
            rate_class,
            rate_threshold,
            timeout,
            force_db_filter,
            post_filter_multiplier,
        )

        with self._query_cache_lock:
            # 超过 maxsize 时简单清空(与 Repo 实例级缓存策略一致,避免无界增长)
            if len(self._query_cache) >= self._query_cache_maxsize:
                self._query_cache.clear()
            self._query_cache[cache_key] = (time.time(), result)
        return result

    @staticmethod
    def build_search_text(intelligence_dict: Dict[str, Any], data_type: str = 'summary') -> str:
        """
        统一的特征文本构建器：将外部字典转换为向量引擎所需的标准文本。
        """
        if data_type == 'summary':
            text_parts = [
                intelligence_dict.get('EVENT_TITLE', ''),
                intelligence_dict.get('EVENT_BRIEF', ''),
                intelligence_dict.get('EVENT_TEXT', '')
            ]
            full_text = "\n\n".join([str(t) for t in text_parts if t and str(t).strip()])
        else:
            raw = intelligence_dict.get('RAW_DATA') or {}
            full_text = raw.get('content', '') or intelligence_dict.get('content', '')

        return full_text
