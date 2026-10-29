# ServiceComponent/ClusterWriteback.py
"""聚类结果写回 MongoDB 归档库（ArchivedData.APPENDIX）。

离线聚合（VectorDB 服务侧）完成后，把 doc_id(UUID) -> cluster_id 的归属映射
以版本化方式写回归档文档的 APPENDIX：

- ``APPENDIX.__CLUSTER_CURRENT__``：当前归属（每次运行覆盖式更新），结构::

    {
      "plan_id": str,                  # 聚合计划 ID
      "version": str,                  # 本次离线聚合版本（幂等键之一）
      "cluster_id": str,               # 簇 ID；噪声点为 "noise"
      "is_noise": bool,                # 是否噪声点（未归入任何簇）
      "cluster_size": int | None,      # 簇大小（噪声为 None）
      "collection_name": str,          # 向量集合名（溯源用）
      "time_range": [float, float] | None,  # 本次聚合时间窗口（秒级时间戳）
      "base_version": str | None,      # 在线状态基线版本（如有）
      "updated_at": datetime,          # 写回时间
    }

- ``APPENDIX.__CLUSTER_HISTORY__``：历史归属列表（追加式），元素结构::

    {"plan_id": str, "version": str, "cluster_id": str, "updated_at": datetime}

行为约定：
- 只处理 doc_to_cluster 映射内的文档；映射之外的归档文档本次不动
  （旧版本归属原样保留，不做清理）；
- 幂等：同一 (plan_id, version) 重复执行不会重复追加历史条目；
  当 current 与历史均已是最新状态时完全不发起写操作；
- 历史长度由 history_keep 截断（默认 DEFAULT_HISTORY_KEEP=20，保留最新 N 条）；
- Mongo 读写失败只记错误日志并计入 failed 统计，绝不抛异常打断聚合流程；
- 读-改-写非原子：聚合为小时级低频任务、单 plan 单写回线程，竞态风险可接受。
"""
from __future__ import annotations

import datetime
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from ServiceComponent.IntelligenceHubDefines_v2 import (
    APPENDIX_CLUSTER_CURRENT,
    APPENDIX_CLUSTER_HISTORY,
)
from Tools.DateTimeUtility import get_aware_time

logger = logging.getLogger(__name__)

# 历史归属默认保留长度（history_keep 未指定时生效）
DEFAULT_HISTORY_KEEP = 20


def _empty_stats(total: int = 0) -> Dict[str, int]:
    return {'total': total, 'updated': 0, 'failed': 0, 'skipped': 0}


def writeback_cluster_appendix(
    mongo_writer: Any,
    *,
    plan_id: str,
    version: Optional[str],
    time_range: Optional[Union[Tuple[float, float], List[float]]] = None,
    collection_name: str = '',
    doc_to_cluster: Optional[Dict[str, str]] = None,
    clusters: Optional[Dict[str, Dict[str, Any]]] = None,
    base_version: Optional[str] = None,
    history_keep: Optional[int] = None,
    now_fn: Optional[Callable[[], datetime.datetime]] = None,
) -> Dict[str, int]:
    """把一次离线聚合的 doc->cluster 归属写回 MongoDB 归档文档 APPENDIX。

    :param mongo_writer: 归档库写入器（MongoDBStorage 或兼容
        ``find_one(query)`` / ``update(filter, data) -> (matched, modified)``
        接口的对象）；写回目标集合由写入器自身绑定（intelligence_archived）。
        为 None 时记 warning 并返回零统计。
    :param plan_id: 聚合计划 ID。
    :param version: 本次离线聚合版本号，与 plan_id 组成幂等键；
        为空时无法保证幂等，记 warning 并放弃写回（返回零统计）。
    :param time_range: 本次聚合的时间窗口（秒级时间戳对），仅作溯源记录。
    :param collection_name: 向量集合名，仅作溯源记录。
    :param doc_to_cluster: 核心映射 {doc_uuid: cluster_id}，噪声点值为 "noise"；
        None/空映射时为 no-op。
    :param clusters: 簇摘要 {cluster_id: {"size": int, ...}}，用于写 cluster_size。
    :param base_version: 在线状态基线版本（可选，溯源记录）。
    :param history_keep: 历史保留长度；None 时使用 DEFAULT_HISTORY_KEEP（20）。
    :param now_fn: 时间源（测试注入用），默认 get_aware_time；
        一次调用内所有文档共用同一时间戳。
    :return: 统计 dict {'total', 'updated', 'failed', 'skipped'}：
        - updated：本次实际写入的文档数；
        - failed：读写抛异常的文档数（只记日志，不抛出）；
        - skipped：归档库中不存在、或已是最新（幂等命中）的文档数。
    """
    stats = _empty_stats()

    if mongo_writer is None:
        logger.warning('聚类写回跳过：mongo_writer 未配置。')
        return stats

    mapping = doc_to_cluster or {}
    stats['total'] = len(mapping)

    if not version:
        logger.warning(f'聚类写回跳过：plan {plan_id} 的聚合结果缺少 version，无法保证幂等。')
        return stats

    if not mapping:
        logger.info(f'聚类写回 no-op：plan {plan_id} version {version} 的 doc_to_cluster 为空。')
        return stats

    keep = DEFAULT_HISTORY_KEEP if history_keep is None else max(1, int(history_keep))
    now = (now_fn or get_aware_time)()
    clusters = clusters or {}
    time_range_record = list(time_range) if time_range else None

    for doc_uuid, cluster_id in mapping.items():
        try:
            outcome = _writeback_single_doc(
                mongo_writer,
                doc_uuid=doc_uuid,
                cluster_id=cluster_id,
                plan_id=plan_id,
                version=version,
                collection_name=collection_name,
                time_range=time_range_record,
                base_version=base_version,
                cluster_size=(clusters.get(cluster_id) or {}).get('size'),
                keep=keep,
                now=now,
            )
            stats[outcome] += 1
        except Exception as e:
            # 单文档失败绝不能打断整批写回，更不能上抛打断聚合监控线程
            stats['failed'] += 1
            logger.error(f'聚类写回失败：UUID={doc_uuid} plan={plan_id} version={version}: {e}')

    logger.info(
        f'聚类写回完成：plan={plan_id} version={version} '
        f'total={stats["total"]} updated={stats["updated"]} '
        f'failed={stats["failed"]} skipped={stats["skipped"]}')
    return stats


def _writeback_single_doc(
    mongo_writer: Any,
    *,
    doc_uuid: str,
    cluster_id: str,
    plan_id: str,
    version: str,
    collection_name: str,
    time_range: Optional[List[float]],
    base_version: Optional[str],
    cluster_size: Optional[int],
    keep: int,
    now: datetime.datetime,
) -> str:
    """写回单个文档，返回统计键名 'updated' / 'skipped'（异常由调用方计入 failed）。"""
    doc = mongo_writer.find_one({'UUID': doc_uuid})
    if not isinstance(doc, dict):
        # 归档库中不存在：映射来自向量库，文档可能尚未归档或已被清理
        return 'skipped'

    appendix = doc.get('APPENDIX')
    if not isinstance(appendix, dict):
        appendix = {}

    old_history_raw = appendix.get(APPENDIX_CLUSTER_HISTORY)
    old_history: List[Dict[str, Any]] = (
        [h for h in old_history_raw if isinstance(h, dict)]
        if isinstance(old_history_raw, list) else []
    )

    old_current = appendix.get(APPENDIX_CLUSTER_CURRENT)
    old_current = old_current if isinstance(old_current, dict) else {}

    # 幂等判定：current 三要素一致、历史已含本 (plan_id, version)、且无需截断
    # → 本次执行不发起任何写操作（重复触发/重放的绝对幂等）。
    already_current = (
        old_current.get('plan_id') == plan_id
        and old_current.get('version') == version
        and old_current.get('cluster_id') == cluster_id
    )
    history_has_entry = any(
        h.get('plan_id') == plan_id and h.get('version') == version
        for h in old_history
    )
    if already_current and history_has_entry and len(old_history) <= keep:
        return 'skipped'

    # 追加历史：先剔除同 (plan_id, version) 旧条目（同版本重跑自愈为最新值，
    # 绝不产生重复条目），再追加本次归属，最后按 keep 截断保留最新。
    entry = {
        'plan_id': plan_id,
        'version': version,
        'cluster_id': cluster_id,
        'updated_at': now,
    }
    new_history = [
        h for h in old_history
        if not (h.get('plan_id') == plan_id and h.get('version') == version)
    ]
    new_history.append(entry)
    new_history = new_history[-keep:]

    current = {
        'plan_id': plan_id,
        'version': version,
        'cluster_id': cluster_id,
        'is_noise': cluster_id == 'noise',
        'cluster_size': cluster_size,
        'collection_name': collection_name,
        'time_range': time_range,
        'base_version': base_version,
        'updated_at': now,
    }

    matched, _modified = mongo_writer.update(
        {'UUID': doc_uuid},
        {
            f'APPENDIX.{APPENDIX_CLUSTER_CURRENT}': current,
            f'APPENDIX.{APPENDIX_CLUSTER_HISTORY}': new_history,
        })
    # matched == 0：find_one 之后文档被删（极端竞态），按 skipped 计
    return 'updated' if matched else 'skipped'
