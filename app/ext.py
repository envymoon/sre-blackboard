"""External connections: connect only when a URL is set and the client library is
installed; otherwise fall back to local stubs (tests and demos keep running).

Production: MYSQL_URL=mysql://.. / REDIS_URL=redis://.. / ES_URL=http://es:9200.
Local fallback: start mysql/redis/elasticsearch via docker compose (see compose file); vendor-neutral.
"""
from __future__ import annotations
from . import settings


def get_mysql():
    if not settings.MYSQL_URL:
        return None
    try:
        import pymysql  # type: ignore
        return pymysql.connect(settings.MYSQL_URL)
    except Exception:
        return None


def get_redis():
    if not settings.REDIS_URL:
        return None
    try:
        import redis  # type: ignore
        return redis.from_url(settings.REDIS_URL, socket_timeout=2)
    except Exception:
        return None


def get_es():
    if not settings.ES_URL:
        return None
    try:
        from elasticsearch import Elasticsearch  # type: ignore
        es = Elasticsearch(settings.ES_URL, request_timeout=8)
        return es if es.ping() else None
    except Exception:
        return None


def backend_status() -> dict:
    return {"mysql": "external" if get_mysql() else "local-stub",
            "redis": "external" if get_redis() else "local-stub",
            "es": "external" if get_es() else "local-stub",
            "reranker": settings.RERANKER_MODE}
