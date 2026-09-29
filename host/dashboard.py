#!/usr/bin/env python3
"""Small local dashboard for live and historical TelosB measurements."""

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, urlparse

from pymongo import MongoClient


MAX_POINTS = 900
MAX_QUERY_ROWS = 20000
VALID_HOURS = (1, 6, 24, 72)


def serialize(document, dataset):
    timestamp = document["timestamp"]
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return {
        "timestamp": timestamp.isoformat(),
        "source_id": document["source_id"],
        "parent_id": document["parent_id"],
        "count": document["count"],
        "temperature_c": document.get("temperature_c"),
        "humidity_pct": document.get("humidity_pct"),
        "photo_raw": document.get("photo_raw"),
        "solar_raw": document.get("solar_raw"),
        "data_origin": dataset,
    }


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    collections = None
    html = None
    forecasts = None

    def send_content(self, status, content, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def send_json(self, value):
        content = json.dumps(value).encode("utf-8")
        self.send_content(200, content, "application/json; charset=utf-8")

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_content(200, self.html, "text/html; charset=utf-8")
            return

        query = parse_qs(parsed.query)
        dataset = query.get("dataset", ["real"])[0]
        if dataset not in self.collections:
            self.send_content(400, b"Invalid dataset", "text/plain")
            return
        collection = self.collections[dataset]

        if parsed.path == "/api/forecasts":
            try:
                source_id = str(int(query["source_id"][0]))
            except (KeyError, ValueError, IndexError):
                self.send_content(400, b"Invalid source", "text/plain")
                return
            snapshot = self.forecasts.get("datasets", {}).get(dataset, {})
            prediction = snapshot.get("sources", {}).get(source_id)
            if prediction is None:
                self.send_content(404, b"No forecast snapshot", "text/plain")
                return
            self.send_json({
                "data_origin": snapshot["data_origin"],
                "dataset_sample_count": snapshot["sample_count"],
                "source": prediction,
            })
            return

        if parsed.path == "/api/latest":
            result = []
            for source_id in sorted(collection.distinct("source_id")):
                document = collection.find_one(
                    {"source_id": source_id}, sort=[("timestamp", -1)]
                )
                if document is not None:
                    result.append(serialize(document, dataset))
            self.send_json(result)
            return

        if parsed.path == "/api/history":
            try:
                source_id = int(query["source_id"][0])
                hours = int(query.get("hours", ["1"])[0])
            except (KeyError, ValueError, IndexError):
                self.send_content(400, b"Invalid query", "text/plain")
                return
            if source_id < 0 or hours not in VALID_HOURS:
                self.send_content(400, b"Invalid range", "text/plain")
                return

            cutoff = datetime.utcnow() - timedelta(hours=hours)
            cursor = collection.find({
                "source_id": source_id,
                "timestamp": {"$gte": cutoff},
            }).sort("timestamp", 1).limit(MAX_QUERY_ROWS)
            rows = list(cursor)
            step = max(1, (len(rows) + MAX_POINTS - 1) // MAX_POINTS)
            selected = rows[::step]
            if rows and selected[-1] is not rows[-1]:
                selected.append(rows[-1])
            self.send_json([serialize(row, dataset) for row in selected])
            return

        self.send_content(404, b"Not found", "text/plain")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--mongo-uri", default="mongodb://127.0.0.1:27017/")
    parser.add_argument("--db", default="iot_wsn")
    args = parser.parse_args()

    client = MongoClient(args.mongo_uri, serverSelectionTimeoutMS=5000)
    client.admin.command("ping")
    Handler.collections = {
        "real": client[args.db].measurements,
        "synthetic": client[args.db].synthetic_measurements,
    }
    with open(os.path.join(os.path.dirname(__file__), "dashboard.html"), "rb") as source:
        Handler.html = source.read()
    forecast_path = os.path.join(os.path.dirname(__file__), "dashboard_forecasts.json")
    try:
        with open(forecast_path, "r", encoding="utf-8") as source:
            Handler.forecasts = json.load(source)
    except FileNotFoundError:
        Handler.forecasts = {}

    server = ThreadedHTTPServer((args.bind, args.port), Handler)
    print("Dashboard listening on {}:{}".format(args.bind, args.port), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        client.close()


if __name__ == "__main__":
    main()
