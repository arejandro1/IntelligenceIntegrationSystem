import re
import traceback

import pytz  # Time zone handling
import logging
import pymongo
import datetime
from bson.objectid import ObjectId
from tzlocal import get_localzone
from typing import Optional, List, Tuple, Union, Dict, Any

from Tools.DateTimeUtility import ensure_timezone_aware
from Tools.MongoDBAccess import MongoDBStorage
from ServiceComponent.IntelligenceHubDefines import APPENDIX_MAX_RATE_SCORE
from ServiceComponent.IntelligenceHubDefines_v2 import APPENDIX_TOTAL_SCORE, APPENDIX_TIME_PUB, APPENDIX_TIME_ARCHIVED


logger = logging.getLogger(__name__)


class IntelligenceQueryEngine:
    def __init__(self, db: MongoDBStorage, max_time_ms: int = 10000):
        self.__mongo_db = db
        # 慢查询保护:find / count_documents 的单次执行时间上限(毫秒),可通过构造参数调整
        self._max_time_ms = max_time_ms

    def get_intelligence(self, _uuid: Union[str, List[str]], light_weight: bool = False) -> Union[Optional[dict], List[dict]]:
        """Retrieve single intelligence entry by UUID

        Args:
            _uuid (Union[str, List[str]]): UUID string or string list to query
            light_weight (bool): Light-weight or full data.

        Returns:
            Union[Optional[dict], List[dict]]:
        """
        is_list_input = isinstance(_uuid, (list, tuple))

        # Parameter validation
        if not _uuid:
            logger.error("UUID parameter is empty")
            return [] if is_list_input else None

        projection = {"RAW_DATA": 0, "EVENT_TEXT": 0} if light_weight else None

        try:
            # Attempt to get database connection
            collection = self.__mongo_db.collection
            if collection is None:
                logger.error("Database connection not initialized")
                return [] if is_list_input else None

            if is_list_input:
                # 清理 UUID 列表：转为小写字符串，并过滤掉空值
                sanitized_uuids = [str(u).lower() for u in _uuid if u]

                if not sanitized_uuids:
                    logger.warning("Input list includes Empty/None UUID")
                    return []

                # 构建 MongoDB $in 查询
                query = {"UUID": {"$in": sanitized_uuids}}

                # 执行查询（find 返回一个游标）
                cursor = collection.find(query, projection=projection)

                # 处理游标中的所有文档
                results = [self.process_document(doc) for doc in cursor]

                # (可选) 记录有多少请求的 UUID 未被找到
                if len(results) < len(sanitized_uuids):
                    found_uuids_set = {doc.get('UUID', '').lower() for doc in results}
                    missing = [u for u in sanitized_uuids if u not in found_uuids_set]
                    logger.warning(f"Not found UUID: {missing}")

                return results
            else:
                # Build exact match UUID query
                query = {"UUID": str(_uuid).lower()}

                # Execute query - get first match
                doc = collection.find_one(query, projection=projection)

                if doc is None:
                    logger.warning(f"No matching UUID found: {_uuid}")
                    return None

                # Process document format
                return self.process_document(doc)

        except pymongo.errors.PyMongoError as e:
            logger.error(f"Database query failed: {str(e)}")
            return [] if is_list_input else None
        except Exception as e:
            logger.exception(f"Unknown error: {str(e)}")
            return [] if is_list_input else None

    def get_intelligence_summary(self) -> Dict[str, Union[int, Optional[str]]]:
        """
        Retrieve total intelligence count and latest document ID as base snapshot

        Returns:
            dict: Dictionary containing:
                - total_count: Total number of intelligence documents
                - base_uuid: UUID of the newest document (as stable pagination base)

        Ensures consistent pagination even when new documents are added.
        """
        collection = self.__mongo_db.collection

        try:
            # Get total document count
            total_count = collection.count_documents({}, maxTimeMS=self._max_time_ms)

            # Find the newest document as base reference
            newest_doc = collection.find_one(
                filter={},
                # TODO: Temporary hardcoded.
                # 加 _id 次级排序:归档时间相同时保证结果确定
                sort=[("APPENDIX.__TIME_ARCHIVED__", pymongo.DESCENDING), ("_id", pymongo.DESCENDING)],
                max_time_ms=self._max_time_ms
            )

            base_uuid = newest_doc["UUID"] if newest_doc else None

            return {
                "total_count": total_count,
                "base_uuid": base_uuid
            }

        except pymongo.errors.PyMongoError as e:
            logger.error(f"Intelligence summary retrieval failed: {str(e)}")
            return {"total_count": 0, "base_uuid": None}

    # def get_paginated_intelligences(self, base_uuid: str, offset: int, limit: int) -> List[dict]:
    #     """
    #     Retrieve paginated intelligence with stable ordering
    #
    #     Args:
    #         base_uuid: Reference UUID for stable pagination anchor (None for start)
    #         offset: Number of documents to skip from the base
    #         limit: Maximum number of documents to return
    #
    #     Returns:
    #         List of processed intelligence documents
    #     """
    #     if limit <= 0:
    #         return []
    #
    #     collection = self.__mongo_db.collection
    #     sort_order = [
    #         # TODO: Temporary hardcoded.
    #         ("APPENDIX.__TIME_ARCHIVED__", pymongo.DESCENDING),
    #         ("_id", pymongo.DESCENDING)  # Secondary sort for consistency
    #     ]
    #
    #     try:
    #         if base_uuid:
    #             base_doc = collection.find_one({"UUID": base_uuid})
    #             if not base_doc:
    #                 logger.warning(f"Base UUID not found: {base_uuid}")
    #                 return []
    #
    #             cursor = collection.find(
    #                 filter={"PUB_TIME": {"$lte": base_doc["PUB_TIME"]}}
    #             ).sort(sort_order).skip(offset).limit(limit)
    #         else:
    #             cursor = collection.find().sort(sort_order).skip(offset).limit(limit)
    #
    #         return [self.process_document(doc) for doc in cursor]
    #
    #     except pymongo.errors.PyMongoError as e:
    #         logger.error(f"Pagination query failed: {str(e)}")
    #         return []
    #
    #     except Exception as e:
    #         logger.error(f"Exception on query: {str(e)}")
    #         return []

    def query_intelligence(
            self,
            *,
            period: Optional[Tuple[datetime.datetime, datetime.datetime]] = None,
            archive_period: Optional[Tuple[datetime.datetime, datetime.datetime]] = None,
            locations: Optional[Union[str, List[str]]] = None,
            peoples: Optional[Union[str, List[str]]] = None,
            organizations: Optional[Union[str, List[str]]] = None,
            keywords: Optional[str] = None,
            threshold: Optional[float] = None,
            threshold_max: Optional[float] = None,
            informant_domains: Optional[Union[str, List[str]]] = None,
            geography: Optional[Union[str, List[str]]] = None,
            skip: Optional[int] = None,
            limit: Optional[int] = None
    ) -> Tuple[List[dict], int]:
        """Execute intelligence query

        Args:
            period: Article mentioned time. UTC time range (start, end)
            archive_period: Archive time. UTC time range (start, end)
            locations: Article mentioned location(s) (str or str list)
            peoples: Article mentioned person (s) (str or str list)
            organizations: Article mentioned organization (s) (str or str list)
            keywords: Keywords in raw article.
            threshold: Minimum score value for filtering APPENDIX_MAX_RATE_SCORE
            threshold_max: Maximum score value for filtering
            geography: Geography region(s) to filter
            skip: Number of documents to skip (offset / page * item_per_page)
            limit: Maximum number of results to return (item_per_page)

        Returns:
            Tuple of
                List of matching intelligence documents
                All document count without specifying skip and limit.
        """
        # Get specified database collection
        collection = self.__mongo_db.collection

        try:
            # Build MongoDB query
            query = self.build_intelligence_query(
                period=period,
                archive_period=archive_period,
                locations=locations,
                peoples=peoples,
                organizations=organizations,
                keywords=keywords,
                threshold=threshold,
                threshold_max=threshold_max,
                informant_domains=informant_domains,
                geography=geography
            )

            compass_query = self.convert_to_compass_query(query)
            logger.debug(compass_query)

            # Execute query and return results with limit
            data = self.execute_query(collection, query, skip=skip, limit=limit)
            total = collection.count_documents(query, maxTimeMS=self._max_time_ms)

            return data, total

        except pymongo.errors.PyMongoError as e:
            logger.error(f"Intelligence query failed: {str(e)}")
            return [], 0
        except Exception as e:
            traceback.print_exc()
            logger.error(f"Intelligence query error: {str(e)}", stack_info=True)
            return [], 0

    def build_intelligence_query(
            self,
            period: Optional[Tuple[datetime.datetime, datetime.datetime]] = None,
            archive_period: Optional[Tuple[datetime.datetime, datetime.datetime]] = None,
            locations: Optional[Union[str, List[str]]] = None,
            peoples: Optional[Union[str, List[str]]] = None,
            organizations: Optional[Union[str, List[str]]] = None,
            keywords: Optional[str] = None,
            threshold: Optional[float] = None,
            threshold_max: Optional[float] = None,
            informant_domains: Optional[Union[str, List[str]]] = None,
            geography: Optional[Union[str, List[str]]] = None
    ) -> dict:
        query_conditions = []

        # 1. Time Period (Compatibility Mode)
        # v1 stores in "PUB_TIME", v2 stores in "APPENDIX.__TIME_PUB__"
        if period:
            # We generate two conditions: one for v1, one for v2
            v1_condition = self.build_time_condition("PUB_TIME", *period)
            v2_condition = self.build_time_condition(f"APPENDIX.{APPENDIX_TIME_PUB}", *period)

            # Use $or operator: matches if the time exists in EITHER field and satisfies range
            query_conditions.append({"$or": [v1_condition, v2_condition]})

        # 2. Archive Period (Stable)
        # Both v1 and v2 store this in APPENDIX
        if archive_period:
            query_conditions.append(self.build_time_condition(f"APPENDIX.{APPENDIX_TIME_ARCHIVED}", *archive_period))

        # 3. Entities (Stable)
        # Both v1 and v2 store these at the root level (LIST[str])
        if locations:
            query_conditions.append(self.build_list_condition("LOCATION", locations))

        if peoples:
            query_conditions.append(self.build_list_condition("PEOPLE", peoples))

        if organizations:
            query_conditions.append(self.build_list_condition("ORGANIZATION", organizations))

        # 4. Keywords (Suggest Updating internal method)
        # Ensure `build_keyword_or_condition` checks fields:
        # ['title', 'content', 'EVENT_TITLE', 'EVENT_BRIEF', 'EVENT_TEXT']
        if keywords:
            query_conditions.append(self.build_keyword_or_condition(keywords))

        # 5. Informant Domain Filter
        if informant_domains:
            query_conditions.append(self.build_informant_condition(informant_domains))

        # 6. Geography Filter (string field, case-insensitive regex match)
        if geography:
            query_conditions.append(self.build_geography_condition(geography))

        # 7. Score Threshold (Hybrid Strategy)
        if threshold is not None or threshold_max is not None:
            v1_score_field = f"APPENDIX.{APPENDIX_MAX_RATE_SCORE}"
            v2_score_field = f"APPENDIX.{APPENDIX_TOTAL_SCORE}"
            # v1 和 v2 的分数是互斥的，用 $or 组合低界，$and 组合高界
            # 最终条件：((v1>=min OR v2>=min) AND (v1<=max OR v2<=max))
            low_conds = []
            high_conds = []
            if threshold is not None:
                low_conds = [{v1_score_field: {"$gte": threshold}}, {v2_score_field: {"$gte": threshold}}]
            if threshold_max is not None:
                high_conds = [{v1_score_field: {"$lte": threshold_max}}, {v2_score_field: {"$lte": threshold_max}}]
            final_score_conds = []
            if low_conds:
                final_score_conds.append({"$or": low_conds})
            if high_conds:
                final_score_conds.append({"$or": high_conds})
            if final_score_conds:
                if len(final_score_conds) == 1:
                    query_conditions.append(final_score_conds[0])
                else:
                    query_conditions.append({"$and": final_score_conds})

        return {"$and": query_conditions} if query_conditions else {}

    # def build_intelligence_query(
    #         self,
    #         period: Optional[Tuple[datetime.datetime, datetime.datetime]] = None,
    #         archive_period: Optional[Tuple[datetime.datetime, datetime.datetime]] = None,
    #         locations: Optional[Union[str, List[str]]] = None,
    #         peoples: Optional[Union[str, List[str]]] = None,
    #         organizations: Optional[Union[str, List[str]]] = None,
    #         keywords: Optional[str] = None,
    #         threshold: Optional[float] = None  # New threshold parameter
    # ) -> dict:
    #     query_conditions = []
    #
    #     if period:
    #         query_conditions.append(self.build_time_condition("PUB_TIME", *period))
    #
    #     if archive_period:
    #         query_conditions.append(self.build_time_condition("APPENDIX.__TIME_ARCHIVED__", *archive_period))
    #
    #     if locations:
    #         query_conditions.append(self.build_list_condition("LOCATION", locations))
    #
    #     if peoples:
    #         query_conditions.append(self.build_list_condition("PEOPLE", peoples))
    #
    #     if organizations:
    #         query_conditions.append(self.build_list_condition("ORGANIZATION", organizations))
    #
    #     if keywords:
    #         query_conditions.append(self.build_keyword_or_condition(keywords))
    #
    #     # Add threshold condition if provided
    #     if threshold is not None:
    #         score_field = f"APPENDIX.__MAX_RATE_SCORE__"
    #         query_conditions.append({score_field: {"$gte": threshold}})
    #
    #     return {"$and": query_conditions} if query_conditions else {}

    def common_query(
            self,
            *,
            conditions: Dict[str, Any],
            operator: str = "$and",
            skip: Optional[int] = None,
            limit: Optional[int] = None
    ) -> List[dict]:
        """
        Execute a dynamic query on the intelligence collection using flexible field conditions.

        This function constructs and executes a MongoDB query based on arbitrary field specifications,
        supporting nested fields (using dot notation) and multiple logical operators. It handles both
        simple equality checks and complex conditions (e.g., range queries, existence checks) dynamically.

        Args:
            conditions: A dictionary mapping field paths to query conditions.
                - Keys can be top-level fields (e.g., "status") or nested paths (e.g., "meta.author.organization").
                - Values can be direct values (implicit equality check) or operator dictionaries (e.g.,
                  {"$gte": 10}, {"$in": ["A", "B"]}, {"$exists": True}).
            operator: Logical operator to combine conditions ("$and" or "$or"). Defaults to "$and".
            skip: Number of documents to skip (for pagination).
            limit: Maximum number of documents to return.

        Returns:
            List[dict]: Matching documents from the collection. Returns empty list on query errors.

        Example:
            conditions = {
                "status": "active",
                "confidence": {"$gte": 0.7},
                "entities.locations": {"$in": ["Beijing", "Shanghai"]},
                "timestamp": {"$exists": True}
            }
            results = dynamic_query(conditions=conditions, operator="$and", skip=0, limit=10)
        """
        collection = self.__mongo_db.collection

        try:
            query = self.build_common_conditions(conditions, operator)
            return self.execute_query(collection, query, skip=skip, limit=limit)

        except pymongo.errors.PyMongoError as e:
            logger.error(f"Dynamic query failed: {str(e)}")
            return []
        except ValueError as e:
            logger.error(f"Invalid operator: {str(e)}")
            return []
        except Exception as e:
            logger.error(f"Dynamic query error: {str(e)}", stack_info=True)
            return []

    def aggregate(self, pipeline: list) -> list:
        try:
            collection = self.__mongo_db.collection
            results = list(collection.aggregate(pipeline))
            return results
        except ValueError:
            logger.error("Invalid time format. Please use ISO format (e.g., '2024-01-01T00:00:00Z')")
            return []
        except Exception as e:
            logger.error(f"Error processing request: {str(e)}")
            return []

    def count_documents(self, _filter: dict) -> int:
        """
        统计匹配过滤条件的文档数量。

        错误契约:查询失败时记录日志并原样抛出异常,不再返回 -1
        (-1 与真实的 "0 条" 无法区分)。调用方(如 IntelligenceHub.count_documents
        及其 RPC 层)需自行捕获并区分 "查询失败" 与 "空结果"。
        """
        try:
            collection = self.__mongo_db.collection
            results = collection.count_documents(_filter, maxTimeMS=self._max_time_ms)
            return results
        except Exception as e:
            logger.error(f"Error processing count_documents request: {str(e)}")
            raise

    def get_latest_archive_timestamp(self) -> Optional[float]:
        """
        获取数据库中最新一条情报的归档时间 (Unix Timestamp)。
        用于离线聚合时的自动时间发现。
        """
        import dateutil.parser  # 确保能解析 ISO 字符串

        collection = self.__mongo_db.collection
        if collection is None:
            return None

        try:
            # 仅投影需要的时间字段，提高查询速度
            latest_record = collection.find_one(
                {},
                projection={f"APPENDIX.{APPENDIX_TIME_ARCHIVED}": 1},
                sort=[(f"APPENDIX.{APPENDIX_TIME_ARCHIVED}", pymongo.DESCENDING), ("_id", pymongo.DESCENDING)],
                max_time_ms=self._max_time_ms
            )

            if latest_record:
                raw_time = latest_record.get('APPENDIX', {}).get(APPENDIX_TIME_ARCHIVED)
                if raw_time:
                    if isinstance(raw_time, datetime.datetime):
                        return raw_time.timestamp()
                    else:
                        return dateutil.parser.parse(str(raw_time)).timestamp()
            return None

        except pymongo.errors.PyMongoError as e:
            logger.error(f"Failed to get latest archive timestamp from MongoDB: {e}")
            return None
        except Exception as e:
            logger.error(f"Error parsing latest archive timestamp: {e}")
            return None

    @staticmethod
    def build_common_conditions(conditions: Dict[str, Any], operator: str = "$and") -> dict:
        """
        Construct MongoDB query conditions from dynamic field specifications.

        Transforms a dictionary of field-based conditions into a valid MongoDB query. Supports:
        - Nested field resolution using dot notation (e.g., "meta.location.city" → nested document query)
        - Explicit operator syntax (e.g., {"$gt": 100})
        - Logical combination of conditions via "$and" or "$or"

        Args:
            conditions: Dictionary of field paths to conditions.
                Each key-value pair generates a condition clause.
            operator: Logical operator ("$and" or "$or") to combine conditions.
                Raises ValueError for invalid operators.

        Returns:
            dict: Valid MongoDB query document. Returns empty dict if no conditions provided.

        Raises:
            ValueError: If operator is not "$and" or "$or".

        Example output for conditions {"a": 1, "b.c": {"$lt": 5}}:
            {"$and": [{"a": 1}, {"b.c": {"$lt": 5}}]}

        Notes:
            - Single-condition inputs return directly without logical operator wrapping.
            - 嵌套字段保持 MongoDB 点路径(dot notation)查询键原样下发,
              不再展开为嵌套文档(展开写法在条件为 {"$gte": x} 等操作符字典时语义错误:
              会被当作对子文档的等值匹配而非范围查询)。
        """
        if operator not in ["$and", "$or"]:
            raise ValueError("Operator must be '$and' or '$or'")

        # 处理嵌套字段和操作符
        condition_list = []
        for field_path, condition in conditions.items():
            # 修正:嵌套字段(含 "." 的路径)直接作为点路径查询键,
            # 例如 "a.b.c" -> {"a.b.c": cond},而不是 {"a": {"b": {"c": cond}}}。
            condition_list.append({field_path: condition})

        # 组合所有条件
        if not condition_list:
            return {}  # 无任何条件
        elif len(condition_list) == 1:
            return condition_list[0]  # 单一条件无需运算符
        else:
            return {operator: condition_list}

    def process_document(self, doc: dict) -> dict:
        doc_processed = self.__mongo_db.process_document_output(doc)

        # 确保所有字段都有默认值
        fields = {
            'TIME': [],
            'LOCATION': [],
            'PEOPLE': [],
            'ORGANIZATION': [],
            'EVENT_BRIEF': "",
            'EVENT_TEXT': "",
            'RATE': {},
            'IMPACT': "",
            'TIPS': ""
        }

        for field, default in fields.items():
            if field not in doc_processed or doc_processed[field] is None:
                doc_processed[field] = default

        return doc_processed

    def build_time_condition(self, key: str, start_time: datetime.datetime, end_time: datetime.datetime) -> dict:
        """
        Builds a MongoDB time condition dictionary, correctly handling timezones
        and returning proper datetime objects for the query.

        Args:
            key: The document field key to apply the condition to.
            start_time: The start of the time range (can be naive or aware).
            end_time: The end of the time range (can be naive or aware).

        Returns:
            A dictionary for MongoDB query with the correct BSON Date type.
        """
        # 1. Ensure both datetime objects are timezone-aware
        aware_start = ensure_timezone_aware(start_time)
        aware_end = ensure_timezone_aware(end_time)

        # 2. Convert both to UTC, keeping them as datetime objects
        utc_start = aware_start.astimezone(pytz.utc)
        utc_end = aware_end.astimezone(pytz.utc)

        # 3. Return the dictionary with the actual datetime objects
        return {key: {"$gte": utc_start, "$lte": utc_end}}

    def build_list_condition(self, field: str, values: Union[str, List[str]]) -> dict:
        """构建列表字段查询条件"""
        target_list = [values] if isinstance(values, str) else values
        return {field: {"$in": target_list}}

    def build_informant_condition(self, domains: Union[str, List[str]]) -> dict:
        """构建数据源域名查询条件。支持单域名或多域名（OR 关系）。"""
        domain_list = [domains] if isinstance(domains, str) else domains
        domain_list = [d.strip().lower() for d in domain_list if d.strip()]
        if not domain_list:
            return {}
        # 对用户输入做 re.escape，防止正则注入与 ReDoS
        if len(domain_list) == 1:
            return {"INFORMANT": {"$regex": re.escape(domain_list[0]), "$options": "i"}}
        return {"$or": [{"INFORMANT": {"$regex": re.escape(d), "$options": "i"}} for d in domain_list]}

    def build_geography_condition(self, regions: Union[str, List[str]]) -> dict:
        """构建地理区域查询条件。GEOGRAPHY 为字符串字段，支持精确或多值（OR 关系）。

        兼容性说明:GEOGRAPHY 字段由 LLM 生成,多国家可能用逗号/分号/空格等
        分隔(如 "美国;日本" 或 "美国 日本")。因此:
        1) 调用方传入的查询串同样先按分隔符拆分为国家名列表;
        2) 多值合并为单一 alternation 正则做子串匹配,不依赖存储侧的具体分隔符。
        """
        raw_list = [regions] if isinstance(regions, str) else regions
        # 拆分逗号/分号/顿号/空白等分隔符(中英文),过滤空段
        region_list = []
        for r in raw_list:
            region_list.extend(p for p in re.split(r"[,;、，；\s]+", r) if p)
        region_list = [r.strip() for r in region_list if r and r.strip()]
        if not region_list:
            return {}
        # 对用户输入做 re.escape，防止正则注入与 ReDoS
        pattern = "|".join(re.escape(r) for r in region_list)
        if len(region_list) == 1:
            return {"GEOGRAPHY": {"$regex": pattern, "$options": "i"}}
        return {"GEOGRAPHY": {"$regex": f"(?:{pattern})", "$options": "i"}}

    def get_source_domains(self, limit: int = 200) -> List[dict]:
        """
        从 MongoDB 聚合提取所有数据源域名及文章数量。
        返回 [{"domain": "bbc.com", "count": 123}, ...]，按 count 降序排列。
        """
        collection = self.__mongo_db.collection
        try:
            pipeline = [
                # 1. 只取有 INFORMANT 的文档
                {"$match": {"INFORMANT": {"$type": "string", "$ne": ""}}},
                # 2. 提取域名：去掉协议头，取主机名
                {
                    "$addFields": {
                        "domain": {
                            "$regexFind": {
                                "input": "$INFORMANT",
                                "regex": "^https?://(?:www\\.)?([^/]+)"
                            }
                        }
                    }
                },
                # 3. 过滤掉解析失败的
                {"$match": {"domain": {"$ne": None}}},
                # 4. 按域名分组计数
                {
                    "$addFields": {
                        "domain_str": {"$arrayElemAt": ["$domain.captures", 0]}
                    }
                },
                {
                    "$group": {
                        "_id": {"$toLower": "$domain_str"},
                        "count": {"$sum": 1}
                    }
                },
                # 5. 排序并限制数量
                {"$sort": {"count": -1}},
                {"$limit": limit},
                # 6. 格式化输出
                {"$project": {"_id": 0, "domain": "$_id", "count": 1}}
            ]
            results = list(collection.aggregate(pipeline))
            return results
        except Exception as e:
            logger.error(f"get_source_domains failed: {e}", exc_info=True)
            return []

    def build_keyword_or_condition(self, keywords: str) -> dict:
        cleaned_keywords = self.sanitize_keywords(keywords)
        # 修复:无有效关键词时返回 {},避免生成非法的 {"$or": []}
        # (空 $or 在 MongoDB 中语义非法/匹配不到任何文档)
        if not cleaned_keywords:
            return {}
        # 平铺所有字段条件（无需二维列表）
        conditions = []
        for kw in cleaned_keywords:
            # ArchivedData 顶层字段(AI 提取结果)
            conditions.append({"EVENT_TITLE": {"$regex": kw, "$options": "i"}})
            conditions.append({"EVENT_BRIEF": {"$regex": kw, "$options": "i"}})
            conditions.append({"EVENT_TEXT": {"$regex": kw, "$options": "i"}})
            # P5 修复:RAW_DATA 存的是 CollectedData,其字段为 title/content,
            # 而非 EVENT_TITLE/EVENT_BRIEF/EVENT_TEXT。原代码查询不存在的字段,
            # 既无法命中又白付 3 条正则全表扫描。
            conditions.append({"RAW_DATA.title": {"$regex": kw, "$options": "i"}})
            conditions.append({"RAW_DATA.content": {"$regex": kw, "$options": "i"}})
        return {"$or": conditions}  # 匹配任一条件

    def build_keyword_and_condition(self, keywords: str) -> dict:
        """构建全文检索查询条件（同时匹配所有关键词）"""
        cleaned_keywords = self.sanitize_keywords(keywords)
        if not cleaned_keywords:
            return {}

        conditions = []
        for kw in cleaned_keywords:
            # 每个关键词在任意字段出现即可（字段间OR）
            kw_condition = {
                "$or": [
                    {"EVENT_BRIEF": {"$regex": kw, "$options": "i"}},
                    {"EVENT_TEXT": {"$regex": kw, "$options": "i"}}
                ]
            }
            conditions.append(kw_condition)  # 每个关键词独立条件组

        # 用AND组合所有关键词条件
        return {"$and": conditions}

    def sanitize_keywords(self, keywords: str) -> List[str]:
        """清洗并优化关键词"""
        # 分割关键词并移除空值
        keywords = [kw.strip() for kw in keywords.split() if kw.strip()]

        # P5 修复:\b 词边界对中文(CJK)无效,会导致中文关键词永远匹配不到。
        # 仅当关键词为纯 ASCII 字母数字时才加 \b;含 CJK 时直接子串匹配。
        def _build_kw(k: str) -> str:
            escaped = re.escape(k)
            # 含 CJK 字符(中日韩统一表意文字及扩展)时不加 \b
            if re.search(r'[\u3000-\u9fff\uf900-\ufaff]', k):
                return escaped
            return r'\b' + escaped + r'\b'
        return [_build_kw(kw) for kw in keywords]

    def execute_query(
            self,
            collection: pymongo.collection.Collection,
            query: dict,
            skip: Optional[int] = None,
            limit: Optional[int] = None
    ) -> List[dict]:
        """Execute query and process results with pagination support

        Args:
            collection: MongoDB collection to query
            query: MongoDB query dictionary
            skip: Number of documents to skip (for pagination)
            limit: Maximum number of documents to return

        Returns:
            List of processed documents matching the query
        """
        try:
            # Apply sorting by TIME field in descending order
            # TODO: Temporary hardcoded.
            cursor = collection.find(query).sort("APPENDIX.__TIME_ARCHIVED__", pymongo.DESCENDING)

            # Apply pagination parameters if provided
            if skip is not None and skip > 0:
                cursor = cursor.skip(skip)  # Skip documents for pagination [6,7](@ref)

            if limit is not None and limit > 0:
                cursor = cursor.limit(limit)  # Limit result size [6](@ref)

            # Process and return results
            return [self.process_document(doc) for doc in cursor]

        except pymongo.errors.PyMongoError as e:
            logger.error(f"MongoDB query execution failed: {str(e)}")
            return []
        except Exception as e:
            logger.error(f"Unexpected error during query execution: {str(e)}", stack_info=True)
            return []

    @staticmethod
    def convert_to_compass_query(query_dict: dict) -> str:
        """
        Converts a Python dictionary to a MongoDB Compass query string.

        This function recursively formats the dictionary and its values to match
        the MongoDB Shell/Compass syntax, properly handling types like
        datetime objects, ObjectIds, strings, numbers, and booleans.

        Args:
            query_dict: A Python dictionary representing the MongoDB query.

        Returns:
            A string that can be directly used in the MongoDB Compass filter bar.
        """

        def format_value(value):
            """Recursively formats a Python value into a Compass-compatible string."""
            if isinstance(value, dict):
                # Recursively format key-value pairs for sub-documents
                items = []
                for k, v in value.items():
                    # Keys in MongoDB are strings, so we quote them.
                    key_str = f'"{k}"'
                    value_str = format_value(v)
                    items.append(f"{key_str}: {value_str}")
                return f"{{ {', '.join(items)} }}"

            elif isinstance(value, list):
                # Recursively format each item in a list
                items = [format_value(item) for item in value]
                return f"[{', '.join(items)}]"

            elif isinstance(value, str):
                # Escape backslashes and double quotes, then wrap in double quotes
                escaped_value = value.replace('\\', '\\\\').replace('"', '\\"')
                return f'"{escaped_value}"'

            elif isinstance(value, datetime.datetime):
                # Format datetime objects into ISODate("...")
                # Ensure it's in UTC format with 'Z'
                iso_string = value.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
                return f'ISODate("{iso_string}")'

            elif isinstance(value, ObjectId):
                # Format ObjectId objects into ObjectId("...")
                return f'ObjectId("{str(value)}")'

            elif isinstance(value, bool):
                # Format booleans as lowercase true/false
                return "true" if value else "false"

            elif isinstance(value, (int, float)):
                # Numbers are converted directly to string
                return str(value)

            elif value is None:
                # None becomes null
                return "null"

            else:
                # For any other unhandled type, convert to a simple string representation
                return f'"{str(value)}"'

        if not isinstance(query_dict, dict):
            raise TypeError("Input must be a dictionary.")

        return format_value(query_dict)
