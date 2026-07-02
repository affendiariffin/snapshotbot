import os

from flask import Flask, jsonify

from server import db

app = Flask(__name__)


@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "snapshotbot"})


@app.get("/")
def index():
    # Task 5 replaces this with the session index page.
    return "Snapshotbot 2.0 — under construction", 200


def main():
    db.init_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=True)


if __name__ == "__main__":
    main()
