"""Tests des helpers du bridge realtime : retry connexion OpenAI, SMS
d'arrière-plan, config VAD.

Le bridge complet (run_realtime_bridge) exige Telnyx + OpenAI réels — ici on
teste les briques extraites, qui portent la logique critique.
"""
import asyncio
from unittest.mock import Mock, patch

from app.services import realtime_service
from app.services.realtime_service import (_connect_openai, _spawn_sms_tasks,
                                           _vad_config)


# ─────────────────────────────────────────
# _vad_config
# ─────────────────────────────────────────


def test_vad_config_contains_full_turn_detection_block():
    cfg = _vad_config(500)
    assert cfg["type"] == "server_vad"
    assert cfg["silence_duration_ms"] == 500
    assert cfg["create_response"] is True
    # threshold et prefix_padding viennent de la config globale
    assert "threshold" in cfg
    assert "prefix_padding_ms" in cfg


# ─────────────────────────────────────────
# _connect_openai (retry + backoff)
# ─────────────────────────────────────────


class _FlakyConnect:
    """Simule websockets.connect : échoue N fois puis retourne un sentinel."""

    def __init__(self, failures: int):
        self.failures = failures
        self.calls = 0
        self.sentinel = object()

    async def __call__(self, *args, **kwargs):
        self.calls += 1
        if self.calls <= self.failures:
            raise OSError("connection refused")
        return self.sentinel


def test_connect_succeeds_first_try():
    fake = _FlakyConnect(failures=0)
    with patch.object(realtime_service.websockets, "connect", fake):
        result = asyncio.run(_connect_openai("test", None, attempts=3, base_delay=0))
    assert result is fake.sentinel
    assert fake.calls == 1


def test_connect_retries_then_succeeds():
    fake = _FlakyConnect(failures=2)
    with patch.object(realtime_service.websockets, "connect", fake):
        result = asyncio.run(_connect_openai("test", None, attempts=3, base_delay=0))
    assert result is fake.sentinel
    assert fake.calls == 3


def test_connect_gives_up_after_attempts():
    fake = _FlakyConnect(failures=99)
    with patch.object(realtime_service.websockets, "connect", fake):
        result = asyncio.run(_connect_openai("test", None, attempts=2, base_delay=0))
    assert result is None
    assert fake.calls == 2


def test_connect_attempts_floor_is_one():
    """attempts=0 mal configuré → quand même 1 tentative."""
    fake = _FlakyConnect(failures=0)
    with patch.object(realtime_service.websockets, "connect", fake):
        result = asyncio.run(_connect_openai("test", None, attempts=0, base_delay=0))
    assert result is fake.sentinel
    assert fake.calls == 1


# ─────────────────────────────────────────
# _spawn_sms_tasks (SMS hors chemin critique)
# ─────────────────────────────────────────


def test_spawn_sms_tasks_sends_all_messages():
    mock_sms = Mock(return_value=True)

    async def run():
        with patch.object(realtime_service, "send_sms", mock_sms):
            _spawn_sms_tasks([("+33612345678", "Résa R4T2K"),
                              ("+33712345678", "Resa R4T2K OK")], "test")
            tasks = list(realtime_service._sms_tasks)
            assert len(tasks) == 2
            await asyncio.gather(*tasks)

    asyncio.run(run())
    assert mock_sms.call_count == 2
    sent = {call.args for call in mock_sms.call_args_list}
    assert ("+33612345678", "Résa R4T2K") in sent
    assert ("+33712345678", "Resa R4T2K OK") in sent


def test_spawn_sms_tasks_failure_does_not_propagate():
    """Un send_sms qui lève ne doit jamais remonter (fire-and-forget loggué)."""
    mock_sms = Mock(side_effect=RuntimeError("provider down"))

    async def run():
        with patch.object(realtime_service, "send_sms", mock_sms):
            _spawn_sms_tasks([("+33612345678", "x")], "test")
            tasks = list(realtime_service._sms_tasks)
            await asyncio.gather(*tasks, return_exceptions=True)
            # Laisse les done-callbacks s'exécuter
            await asyncio.sleep(0)

    asyncio.run(run())  # ne lève pas
    assert mock_sms.call_count == 1


def test_spawn_sms_tasks_cleans_registry():
    """Les réfs fortes sont relâchées une fois les tâches terminées."""
    async def run():
        with patch.object(realtime_service, "send_sms", Mock(return_value=True)):
            _spawn_sms_tasks([("+33612345678", "x")], "test")
            await asyncio.gather(*list(realtime_service._sms_tasks))
            await asyncio.sleep(0)

    asyncio.run(run())
    assert not realtime_service._sms_tasks
