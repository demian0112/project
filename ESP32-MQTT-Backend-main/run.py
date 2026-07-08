from pathlib import Path

from dotenv import load_dotenv

from app import create_app


load_dotenv(Path(__file__).with_name(".env"), override=True)


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=False)
