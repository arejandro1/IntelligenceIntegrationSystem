import datetime
from mongodb_exporter import export_mongodb_data


def get_current_month_range() -> (str, str):
    """获取当前月的起始和结束时间（ISO格式）[1,7](@ref)"""
    # datetime.utcnow() 已弃用（Python 3.12+）；改用带时区的 now(UTC)。
    # 仅取 year/month 参与计算，输出仍为 naive ISO 串 + "Z" 后缀（UTC 语义不变）。
    today = datetime.datetime.now(datetime.timezone.utc)
    first_day = datetime.datetime(today.year, today.month, 1)
    last_day = first_day + datetime.timedelta(days=32)
    last_day = datetime.datetime(last_day.year, last_day.month, 1) - datetime.timedelta(seconds=1)
    return first_day.isoformat() + "Z", last_day.isoformat() + "Z"


if __name__ == "__main__":
    # 生成带时间戳的文件名
    timestamp = datetime.datetime.now().strftime("%Y%m%d")
    output_file = f"intelligence_archived_month_{timestamp}.json"

    # 获取本月时间范围（ISO格式）[1,6](@ref)
    start_date, end_date = get_current_month_range()

    # 构建本月查询条件
    date_query = {
        "PUB_TIME": {
            "$gte": {"$date": start_date},
            "$lte": {"$date": end_date}
        }
    }

    # 导出 IntelligenceIntegrationSystem 数据库中的 intelligence_archived 集合本月记录
    export_mongodb_data(
        uri="mongodb://localhost:27017",  # 根据实际情况修改
        db="IntelligenceIntegrationSystem",
        collection="intelligence_archived",
        output_file=output_file,
        query=date_query,
        export_format="json"
    )
