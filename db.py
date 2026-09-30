import os
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

load_dotenv()


def get_db():
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        raise RuntimeError("No se encontró DATABASE_URL")

    return psycopg2.connect(
        database_url,
        cursor_factory=RealDictCursor
    )