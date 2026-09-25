import asyncio
import datetime

from sqlalchemy import create_engine

from studysystem.server import create_server


def test_study_ping_returns_utc_iso_time() -> None:
    server = create_server(create_engine("sqlite://"))  # ping never touches it

    tools = asyncio.run(server.list_tools())
    assert "study_ping" in [t.name for t in tools]

    result = asyncio.run(server.call_tool("study_ping", {}))
    text = result.content[0].text
    stamp = datetime.datetime.fromisoformat(text)
    assert stamp.tzinfo is not None
    assert stamp.utcoffset() == datetime.timedelta(0)
