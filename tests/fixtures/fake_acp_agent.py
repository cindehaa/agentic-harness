"""A scripted ACP agent for tests: the prompt's first word picks what it does."""

import json
import sys
import threading

lock = threading.Lock()
replies: dict[int, dict] = {}
arrived = threading.Condition(lock)
cancelled = threading.Event()
next_id = 1000
state = {"turn": 0, "new": None}


def send(message: dict) -> None:
    with lock:
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()


def update(session: str, u: dict) -> None:
    send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": session, "update": u}})


def ask(method: str, params: dict) -> dict:
    global next_id
    with lock:
        next_id += 1
        id_ = next_id
    send({"jsonrpc": "2.0", "id": id_, "method": method, "params": params})
    with arrived:
        arrived.wait_for(lambda: id_ in replies)
        return replies.pop(id_)


def text(session: str, words: str) -> None:
    update(session, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": words}})


def prompt(id_: int, params: dict) -> None:
    session = params["sessionId"]
    words = " ".join(b["text"] for b in params["prompt"] if b["type"] == "text").split()
    state["turn"] += 1
    command, rest = (words[0] if words else ""), " ".join(words[1:])
    usage = {"inputTokens": 100, "outputTokens": 20, "cachedReadTokens": 50, "cachedWriteTokens": 0, "totalTokens": 170}
    result = {"stopReason": "end_turn", "usage": usage}
    if command == "work":
        text(session, "Let me look.")
        update(session, {"sessionUpdate": "tool_call", "toolCallId": f"t{state['turn']}", "title": "ls", "kind": "execute",
                         "status": "pending"})
        update(session, {"sessionUpdate": "tool_call_update", "toolCallId": f"t{state['turn']}", "rawInput": {"command": "ls"}})
        answer = ask("session/request_permission", {"sessionId": session, "toolCall": {"toolCallId": f"t{state['turn']}"}, "options": [
            {"optionId": "always", "name": "Always", "kind": "allow_always"},
            {"optionId": "once", "name": "Once", "kind": "allow_once"},
            {"optionId": "no", "name": "No", "kind": "reject_once"}]})
        chosen = answer["result"]["outcome"].get("optionId")
        update(session, {"sessionUpdate": "tool_call_update", "toolCallId": f"t{state['turn']}", "status": "completed",
                         "rawOutput": f"a.txt (permission: {chosen})"})
        update(session, {"sessionUpdate": "usage_update", "used": 1000, "size": 200000,
                         "cost": {"amount": 0.01 * state["turn"], "currency": "USD"}})
        text(session, f"done {state['turn']} {rest}")
    elif command == "setup":
        text(session, json.dumps(state["new"]))
    elif command in ("hang", "spend"):
        if command == "spend":
            update(session, {"sessionUpdate": "usage_update", "used": 1, "size": 2, "cost": {"amount": 5.0, "currency": "USD"}})
        cancelled.clear()
        cancelled.wait()
        if rest == "hard":
            threading.Event().wait()
        result["stopReason"] = "cancelled"
    elif command == "refuse":
        result["stopReason"] = "refusal"
    elif command == "fail":
        send({"jsonrpc": "2.0", "id": id_, "error": {"code": -32603, "message": rest}})
        return
    elif command == "crash":
        sys.stdout.flush()
        import os
        os._exit(3)
    elif command == "files":
        answer = ask("fs/read_text_file", {"sessionId": session, "path": "/etc/hostname"})
        text(session, json.dumps(answer.get("error")))
    send({"jsonrpc": "2.0", "id": id_, "result": result})


def main() -> None:
    for line in sys.stdin:
        message = json.loads(line)
        method, id_ = message.get("method"), message.get("id")
        if method is None:
            with arrived:
                replies[id_] = message
                arrived.notify_all()
        elif method == "initialize":
            send({"jsonrpc": "2.0", "id": id_, "result": {
                "protocolVersion": 1, "agentInfo": {"name": "fake", "version": "1"},
                "agentCapabilities": {"mcpCapabilities": {"http": "nohttp" not in sys.argv}}}})
        elif method == "session/new":
            state["new"] = message["params"]
            send({"jsonrpc": "2.0", "id": id_, "result": {"sessionId": "s1"}})
        elif method == "session/prompt":
            threading.Thread(target=prompt, args=(id_, message["params"]), daemon=True).start()
        elif method == "session/cancel":
            cancelled.set()


if __name__ == "__main__":
    main()
