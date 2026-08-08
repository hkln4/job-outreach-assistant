"""One-off: create data/app.db with all tables. `python -m scripts.init_db`"""
from app.db import init_db

if __name__ == "__main__":
    init_db()
    print("DB initialized.")
