import re
import logging
import pymongo
import datetime
from flask import jsonify
from typing import Optional, List, Tuple, Union, Dict, Any

from Tools.DateTimeUtility import ensure_timezone_aware
from Tools.MongoDBAccess import MongoDBStorage
from ServiceComponent.IntelligenceHubDefines import APPENDIX_TIME_ARCHIVED, APPENDIX_MAX_RATE_SCORE
from ServiceComponent.IntelligenceHubDefines_v2 import APPENDIX_TOTAL_SCORE

logger = logging.getLogger(__name__)


class IntelligenceStatisticsEngine:
    def __init__(self, db: MongoDBStorage):
        self.__mongo_db = db
        try:
            from tzlocal import get_localzone_name
            self.__local_timezone = get_localzone_name()
            print(f"StatisticsEngine: Using local timezone: {self.__local_timezone}")
        except Exception as e:
            print(f"Warning: Could not determine local timezone. Falling back to UTC. Error: {e}")
            self.__local_timezone = "UTC"

    def get_score_distribution(self, start_time: datetime.datetime, end_time: datetime.datetime):
        """
        API endpoint to get score distribution within a specified time range
        Expected query parameters:
        - start_time: ISO format start timestamp (e.g., '2024-01-01T00:00:00Z')
        - end_time: ISO format end timestamp (e.g., '2024-12-31T23:59:59Z')
        """
        # try:

        # MongoDB 聚合：v2 使用 __TOTAL_SCORE__，v1 使用 __MAX_RATE_SCORE__。
        # 用 $ifNull 兼容两者，并用 $floor 取整分组，避免一位小数（如 7.5）被静默丢弃。
        pipeline = [
            {
                "$match": {
                    f"APPENDIX.{APPENDIX_TIME_ARCHIVED}": {
                        "$gte": ensure_timezone_aware(start_time),
                        "$lte": ensure_timezone_aware(end_time)
                    },
                    "$expr": {
                        "$and": [
                            {
                                "$gte": [
                                    {"$ifNull": [f"$APPENDIX.{APPENDIX_TOTAL_SCORE}",
                                                 f"$APPENDIX.{APPENDIX_MAX_RATE_SCORE}",
                                                 0]},
                                    0
                                ]
                            },
                            {
                                "$lte": [
                                    {"$ifNull": [f"$APPENDIX.{APPENDIX_TOTAL_SCORE}",
                                                 f"$APPENDIX.{APPENDIX_MAX_RATE_SCORE}",
                                                 0]},
                                    10
                                ]
                            }
                        ]
                    }
                }
            },
            {
                "$group": {
                    "_id": {
                        "$floor": {
                            "$ifNull": [f"$APPENDIX.{APPENDIX_TOTAL_SCORE}",
                                        f"$APPENDIX.{APPENDIX_MAX_RATE_SCORE}",
                                        0]
                        }
                    },
                    "count": {"$sum": 1}
                }
            },
            {
                "$sort": {"_id": 1}
            }
        ]

        collection = self.__mongo_db.collection
        results = collection.aggregate(pipeline)

        score_distribution = {str(i): 0 for i in range(1, 11)}  # Initialize all scores 1-10 with count 0

        for result in results:
            score = int(result['_id'])
            if 1 <= score <= 10:
                score_distribution[str(score)] = result['count']

        return score_distribution

    def get_hourly_stats(self, start_time: datetime.datetime, end_time: datetime.datetime) -> list:
        """Get record counts grouped by hour for the specified time range"""
        # MongoDB aggregation pipeline for hourly statistics

        date_in_local_tz = {
            "date": "$APPENDIX.__TIME_ARCHIVED__",
            "timezone": self.__local_timezone  # <-- 在这里使用本地时区
        }

        pipeline = [
            {
                "$match": {
                    "APPENDIX.__TIME_ARCHIVED__": {
                        "$gte": ensure_timezone_aware(start_time),
                        "$lte": ensure_timezone_aware(end_time)
                    }
                }
            },
            {
                "$group": {
                    "_id": {
                        "year": {"$year": date_in_local_tz},
                        "month": {"$month": date_in_local_tz},
                        "day": {"$dayOfMonth": date_in_local_tz},
                        "hour": {"$hour": date_in_local_tz}
                    },
                    "count": {"$sum": 1}
                }
            },
            {
                "$sort": {
                    "_id.year": 1,
                    "_id.month": 1,
                    "_id.day": 1,
                    "_id.hour": 1
                }
            }
        ]

        collection = self.__mongo_db.collection
        return list(collection.aggregate(pipeline))

    def get_daily_stats(self, start_time: datetime.datetime, end_time: datetime.datetime):
        """Get record counts grouped by day for the specified time range"""
        # MongoDB aggregation pipeline for daily statistics

        date_in_local_tz = {
            "date": "$APPENDIX.__TIME_ARCHIVED__",
            "timezone": self.__local_timezone
        }

        pipeline = [
            {
                "$match": {
                    "APPENDIX.__TIME_ARCHIVED__": {
                        "$gte": ensure_timezone_aware(start_time),
                        "$lte": ensure_timezone_aware(end_time)
                    }
                }
            },
            {
                "$group": {
                    "_id": {
                        "year": {"$year": date_in_local_tz},
                        "month": {"$month": date_in_local_tz},
                        "day": {"$dayOfMonth": date_in_local_tz}
                    },
                    "count": {"$sum": 1}
                }
            },
            {
                "$sort": {
                    "_id.year": 1,
                    "_id.month": 1,
                    "_id.day": 1
                }
            }
        ]

        collection = self.__mongo_db.collection
        return list(collection.aggregate(pipeline))

        # return jsonify(result)

    def get_weekly_stats(self, start_time: datetime.datetime, end_time: datetime.datetime):
        """Get record counts grouped by week for the specified time range"""
        # MongoDB aggregation pipeline for weekly statistics

        date_in_local_tz = {
            "date": "$APPENDIX.__TIME_ARCHIVED__",
            "timezone": self.__local_timezone
        }

        pipeline = [
            {
                "$match": {
                    "APPENDIX.__TIME_ARCHIVED__": {
                        "$gte": ensure_timezone_aware(start_time),
                        "$lte": ensure_timezone_aware(end_time)
                    }
                }
            },
            {
                "$group": {
                    "_id": {
                        "year": {"$year": date_in_local_tz},
                        "week": {"$week": date_in_local_tz}
                    },
                    "count": {"$sum": 1}
                }
            },
            {
                "$sort": {
                    "_id.year": 1,
                    "_id.week": 1
                }
            }
        ]

        collection = self.__mongo_db.collection
        return list(collection.aggregate(pipeline))

    def get_monthly_stats(self, start_time: datetime.datetime, end_time: datetime.datetime):
        """Get record counts grouped by month for the specified time range"""
        # MongoDB aggregation pipeline for monthly statistics

        date_in_local_tz = {
            "date": "$APPENDIX.__TIME_ARCHIVED__",
            "timezone": self.__local_timezone
        }

        pipeline = [
            {
                "$match": {
                    "APPENDIX.__TIME_ARCHIVED__": {
                        "$gte": ensure_timezone_aware(start_time),
                        "$lte": ensure_timezone_aware(end_time)
                    }
                }
            },
            {
                "$group": {
                    "_id": {
                        "year": {"$year": date_in_local_tz},
                        "month": {"$month": date_in_local_tz}
                    },
                    "count": {"$sum": 1}
                }
            },
            {
                "$sort": {
                    "_id.year": 1,
                    "_id.month": 1
                }
            }
        ]

        collection = self.__mongo_db.collection
        return list(collection.aggregate(pipeline))

    def get_stats_summary(self, start_time: datetime.datetime, end_time: datetime.datetime) -> Tuple[int, list]:
        """Get overall statistics for the specified time range"""
        collection = self.__mongo_db.collection

        # Total count in time range
        total_count = self.__mongo_db.collection.count_documents({
            "APPENDIX.__TIME_ARCHIVED__": {
                "$gte": ensure_timezone_aware(start_time),
                "$lte": ensure_timezone_aware(end_time)
            }
        })

        # Count by informant
        informant_pipeline = [
            {
                "$match": {
                    "APPENDIX.__TIME_ARCHIVED__": {
                        "$gte": ensure_timezone_aware(start_time),
                        "$lte": ensure_timezone_aware(end_time)
                    }
                }
            },
            {
                "$group": {
                    "_id": "$INFORMANT",
                    "count": {"$sum": 1}
                }
            },
            {
                "$sort": {"count": -1}
            },
            {
                "$limit": 10  # Top 10 informants
            }
        ]

        informant_stats = list(collection.aggregate(informant_pipeline))

        return total_count, informant_stats
