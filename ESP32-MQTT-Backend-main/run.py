from pathlib import Path
import os

from dotenv import load_dotenv

from app import create_app


load_dotenv(Path(__file__).with_name(".env"), override=True)


app = create_app()


if __name__ == "__main__":
    port = int(os.getenv("FLASK_RUN_PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True, use_reloader=False)
