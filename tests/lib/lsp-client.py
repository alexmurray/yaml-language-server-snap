#!/usr/bin/env python3
"""Minimal LSP client that drives yaml-language-server through the requests
an editor makes and checks its responses against the test workspace.

Usage: lsp-client.py [--socket] [--schema-url URL] WORKSPACE

The server is spawned with --stdio, or with --socket it is instead told to
connect back to a TCP port this client listens on. With --schema-url, a
document that names that URL as its schema is also validated, so the server
has to fetch the schema over the network.
"""

import argparse
import json
import os
import pathlib
import socket
import subprocess
import sys
import time

TIMEOUT = 60


class Client:
    def __init__(self, reader, writer, settings):
        self.reader = reader
        self.writer = writer
        self.settings = settings
        self.next_id = 0
        self.diagnostics = {}

    def send(self, msg):
        msg["jsonrpc"] = "2.0"
        body = json.dumps(msg).encode()
        self.writer.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
        self.writer.flush()

    def read(self):
        length = None
        while True:
            line = self.reader.readline()
            if not line:
                raise RuntimeError("server closed the connection")
            line = line.strip()
            if not line:
                break
            name, _, value = line.decode().partition(":")
            if name.lower() == "content-length":
                length = int(value)
        return json.loads(self.reader.read(length))

    def handle(self, msg):
        """Handle a server-initiated request or notification."""
        method = msg.get("method")
        if method == "textDocument/publishDiagnostics":
            params = msg["params"]
            self.diagnostics[params["uri"]] = params["diagnostics"]
        if "id" not in msg:
            return
        result = None
        if method == "workspace/configuration":
            result = [
                self.settings if item.get("section") == "yaml" else None
                for item in msg["params"]["items"]
            ]
        self.send({"id": msg["id"], "result": result})

    def request(self, method, params):
        self.next_id += 1
        rid = self.next_id
        self.send({"id": rid, "method": method, "params": params})
        while True:
            msg = self.read()
            if msg.get("id") == rid and "method" not in msg:
                if "error" in msg:
                    raise RuntimeError("%s failed: %s" % (method, msg["error"]))
                return msg["result"]
            self.handle(msg)

    def notify(self, method, params):
        self.send({"method": method, "params": params})

    def wait_diagnostics(self, uri, check):
        """Process messages until uri's diagnostics satisfy check()."""
        deadline = time.monotonic() + TIMEOUT
        while not (uri in self.diagnostics and check(self.diagnostics[uri])):
            if time.monotonic() > deadline:
                raise RuntimeError(
                    "timed out waiting for diagnostics for %s, last: %s"
                    % (uri, self.diagnostics.get(uri))
                )
            self.handle(self.read())
        return self.diagnostics[uri]


def check(cond, msg, *args):
    if not cond:
        raise RuntimeError(msg % args)


def messages(diags):
    return [d["message"] for d in diags]


def apply_edits(text, edits):
    lines = text.splitlines(keepends=True)

    def offset(pos):
        return sum(len(l) for l in lines[: pos["line"]]) + pos["character"]

    for edit in sorted(edits, key=lambda e: offset(e["range"]["start"]), reverse=True):
        start = offset(edit["range"]["start"])
        end = offset(edit["range"]["end"])
        text = text[:start] + edit["newText"] + text[end:]
    return text


def run(client, root, schema_url):
    root_uri = root.as_uri()
    result = client.request(
        "initialize",
        {
            "processId": os.getpid(),
            "rootUri": root_uri,
            "workspaceFolders": [{"uri": root_uri, "name": root.name}],
            "capabilities": {
                "workspace": {"configuration": True},
                "textDocument": {
                    "hover": {"contentFormat": ["markdown", "plaintext"]},
                    "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                    "publishDiagnostics": {},
                },
            },
        },
    )
    caps = result["capabilities"]
    print("capabilities: %s" % sorted(caps))
    for cap in ("hoverProvider", "completionProvider", "documentSymbolProvider"):
        check(caps.get(cap), "server does not advertise %s", cap)
    client.notify("initialized", {})
    client.notify("workspace/didChangeConfiguration", {"settings": {"yaml": client.settings}})

    versions = {}

    def open_doc(name, text=None):
        path = root / name
        if text is None:
            text = path.read_text()
        uri = path.as_uri()
        versions[uri] = 1
        client.notify(
            "textDocument/didOpen",
            {"textDocument": {"uri": uri, "languageId": "yaml", "version": 1, "text": text}},
        )
        return uri

    # a document which satisfies its schema (read from the workspace, so the
    # server needs access to the user's home directory) has no diagnostics
    pet = open_doc("pet.yaml")
    diags = client.wait_diagnostics(pet, lambda d: True)
    check(not diags, "unexpected diagnostics for pet.yaml: %s", diags)

    # whereas one which violates it reports each violation
    broken = open_doc("broken.yaml")
    expected = ['Value is not accepted. Valid values: "cat", "dog", "fish".',
                'Incorrect type. Expected "integer".']
    diags = client.wait_diagnostics(
        broken, lambda d: all(m in messages(d) for m in expected)
    )
    print("diagnostics: %s" % messages(diags))

    # malformed YAML is reported too
    syntax = open_doc("syntax.yaml", "name: [Rex\nkind: dog\n")
    diags = client.wait_diagnostics(syntax, lambda d: len(d) > 0)
    print("syntax diagnostics: %s" % messages(diags))

    # hover shows the schema's description of the key - 0-based positions,
    # so line 2 is `kind: dog`
    hover = client.request(
        "textDocument/hover",
        {"textDocument": {"uri": pet}, "position": {"line": 2, "character": 1}},
    )
    print("hover: %s" % hover)
    check(hover and "What sort of animal the pet is" in json.dumps(hover),
          "unexpected hover result: %s", hover)

    # completion offers the enum values for a key's value...
    partial = open_doc("partial.yaml", "# yaml-language-server: $schema=./schema.json\nname: Rex\nkind: \n")
    result = client.request(
        "textDocument/completion",
        {"textDocument": {"uri": partial}, "position": {"line": 2, "character": 6}},
    )
    items = result["items"] if isinstance(result, dict) else result
    labels = sorted(i["label"] for i in items)
    print("value completions: %s" % labels)
    check(labels == ["cat", "dog", "fish"], "unexpected value completions: %s", labels)

    # ... and the not yet used properties for a new key
    result = client.request(
        "textDocument/completion",
        {"textDocument": {"uri": partial}, "position": {"line": 3, "character": 0}},
    )
    items = result["items"] if isinstance(result, dict) else result
    labels = sorted(i["label"] for i in items)
    print("key completions: %s" % labels)
    check("age" in labels and "name" not in labels, "unexpected key completions: %s", labels)

    # the outline lists each top-level key
    symbols = client.request("textDocument/documentSymbol", {"textDocument": {"uri": pet}})
    names = [s["name"] for s in symbols]
    print("symbols: %s" % names)
    check(names == ["name", "kind", "age"], "unexpected document symbols: %s", names)

    # formatting tidies up a messy document
    messy = "name:    Rex\nkind:   dog\nage:   3\n"
    ugly = open_doc("ugly.yaml", messy)
    edits = client.request(
        "textDocument/formatting",
        {"textDocument": {"uri": ugly}, "options": {"tabSize": 2, "insertSpaces": True}},
    )
    formatted = apply_edits(messy, edits or [])
    print("formatted: %r" % formatted)
    check(formatted == "name: Rex\nkind: dog\nage: 3\n", "unexpected formatting: %r", formatted)

    # a schema fetched over the network is applied too
    if schema_url:
        remote = open_doc(
            "remote.yaml",
            "# yaml-language-server: $schema=%s\nname: Rex\nkind: dragon\n" % schema_url,
        )
        diags = client.wait_diagnostics(remote, lambda d: expected[0] in messages(d))
        print("remote schema diagnostics: %s" % messages(diags))

    client.request("shutdown", None)
    client.notify("exit", None)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", action="store_true")
    parser.add_argument("--schema-url")
    parser.add_argument("workspace")
    args = parser.parse_args()

    root = pathlib.Path(args.workspace).resolve()
    settings = {
        "validate": True,
        "hover": True,
        "completion": True,
        "format": {"enable": True},
        # keep the tests independent of the internet
        "schemaStore": {"enable": False},
    }

    if args.socket:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(TIMEOUT)
        port = listener.getsockname()[1]
        server = subprocess.Popen(
            ["yaml-language-server", "--socket=%d" % port],
            stdin=subprocess.DEVNULL, stdout=sys.stderr,
        )
        conn, _ = listener.accept()
        listener.close()
        conn.settimeout(TIMEOUT)
        reader = conn.makefile("rb")
        writer = conn.makefile("wb")
    else:
        server = subprocess.Popen(
            ["yaml-language-server", "--stdio"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        )
        reader = server.stdout
        writer = server.stdin

    try:
        run(Client(reader, writer, settings), root, args.schema_url)
        server.wait(timeout=TIMEOUT)
    finally:
        if server.poll() is None:
            server.kill()
    print("server exited with %d" % server.returncode)
    return server.returncode


if __name__ == "__main__":
    sys.exit(main())
