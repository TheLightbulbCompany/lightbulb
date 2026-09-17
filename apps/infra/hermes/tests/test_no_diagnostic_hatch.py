import pathlib

SERVER = pathlib.Path(__file__).resolve().parents[1] / "bridge" / "server.py"


def test_no_shell_execution_path_survives_in_the_bridge():
    source = SERVER.read_text(encoding="utf-8")
    for banned in ("subprocess", "diagnostic", "shell=True"):
        assert banned not in source, f"{banned} still present in server.py"


def test_the_throwaway_ws_echo_is_gone():
    source = SERVER.read_text(encoding="utf-8")
    for banned in ("_ws_echo", "Sec-WebSocket-Accept", "258EAFA5"):
        assert banned not in source
