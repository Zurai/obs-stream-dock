"""
YouTube live chat reader (runs as a separate process, started by obs_stream_dock.py).

Why a separate process: liveChatMessages.streamList only exists as a gRPC
server-streaming call. Keeping gRPC out of the OBS process means a crash or
hang in its native code can never take OBS down mid-stream.

Protocol:
  stdin   one JSON line: {"token": "...", "live_chat_id": "...", "page_token": "..." (optional)}
          The pipe stays open. When it closes (OBS exited or the script was
          unloaded), this process exits immediately.
  stdout  one JSON line per event:
          {"type": "page", "nextPageToken": ..., "offlineAt": ..., "items": [...]}
          {"type": "error", "code": "UNAUTHENTICATED", "message": "..."}
          {"type": "end"}
"""

import json
import os
import sys
import threading

import grpc
from google.protobuf import json_format

import stream_list_pb2
import stream_list_pb2_grpc

ENDPOINT = "youtube.googleapis.com:443"


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def watch_parent():
    # Our stdin is a pipe from OBS. EOF means OBS is gone or wants us gone.
    try:
        while sys.stdin.readline():
            pass
    finally:
        os._exit(0)


def main():
    # Pipes on Windows default to the ANSI code page (cp1252), which can't
    # encode emoji. Chat is full of them, so force UTF-8 both ways.
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    line = sys.stdin.readline()
    if not line:
        return
    cfg = json.loads(line)
    threading.Thread(target=watch_parent, daemon=True).start()

    request = stream_list_pb2.LiveChatMessageListRequest(
        live_chat_id=cfg["live_chat_id"],
        part=["snippet", "authorDetails"],
        max_results=500,
    )
    if cfg.get("page_token"):
        request.page_token = cfg["page_token"]

    options = [
        ("grpc.keepalive_time_ms", 60_000),
        ("grpc.keepalive_timeout_ms", 20_000),
    ]
    with grpc.secure_channel(ENDPOINT, grpc.ssl_channel_credentials(), options=options) as channel:
        stub = stream_list_pb2_grpc.V3DataLiveChatMessageServiceStub(channel)
        metadata = (("authorization", "Bearer " + cfg["token"]),)
        try:
            for response in stub.StreamList(request, metadata=metadata):
                d = json_format.MessageToDict(response)
                emit({
                    "type": "page",
                    "nextPageToken": d.get("nextPageToken"),
                    "offlineAt": d.get("offlineAt"),
                    "items": d.get("items", []),
                })
        except grpc.RpcError as e:
            emit({"type": "error", "code": e.code().name, "message": e.details() or ""})
            return 2
    emit({"type": "end"})
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
