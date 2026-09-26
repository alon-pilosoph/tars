"""Streaming speech to text's safety net: if the stream fails, OpenAI transcribes the same recording."""

from voice_assistant.stt import BufferedSession, FallbackTranscriber


class Batch:
    def __init__(self, text="from the backup"):
        self.text, self.heard = text, []

    def session(self):
        return BufferedSession(self)

    def transcribe(self, pcm):
        self.heard.append(pcm)
        return self.text


class BrokenStream:
    def session(self):
        return self

    def feed(self, pcm):
        pass

    def finish(self):
        raise ConnectionError("the stream dropped")

    def cancel(self):
        pass


def test_a_dropped_stream_is_transcribed_by_the_backup_from_the_same_audio():
    backup = Batch()
    session = FallbackTranscriber(BrokenStream(), backup).session()
    session.feed(b"12")
    session.feed(b"34")
    assert session.finish() == "from the backup" and backup.heard == [b"1234"]


def test_a_working_stream_never_calls_the_backup():
    backup = Batch()
    session = FallbackTranscriber(Batch("streamed"), backup).session()
    session.feed(b"12")
    assert session.finish() == "streamed" and backup.heard == []
