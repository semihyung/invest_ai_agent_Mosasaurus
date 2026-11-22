import os
import time
from pathlib import Path

from src.db import initialize_database


def main() -> None:
    db_path = Path(os.environ.get("DB_PATH", "/app/data/mosasaurus.db"))
    connection = initialize_database(db_path)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        connection.close()


if __name__ == "__main__":
    main()
