"""Unit tests for the agent harness.

Run with::

    python -m unittest agent_test -v
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import types
import unittest
import unittest.mock
from pathlib import Path

import agent as A


def run(coro):
    return asyncio.run(coro)


class ConfigTest(unittest.TestCase):
    def test_merge_keeps_known_fields_and_collects_extras(self):
        config = A.AgentConfig().merge(model="m", genre="noir", ignored=None)
        self.assertEqual(config.model, "m")
        self.assertEqual(config.extras["genre"], "noir")
        self.assertNotIn("ignored", config.extras)

    def test_extras_are_readable_as_attributes(self):
        config = A.AgentConfig(extras={"genre": "noir"})
        self.assertEqual(config.genre, "noir")
        with self.assertRaises(AttributeError):
            _ = config.missing

    def test_env_layer_coerces_types(self):
        env = {"AGENT_PORT": "9100", "AGENT_QUIET": "true", "AGENT_MODEL": "env-model"}
        config = A.AgentConfig().with_env(env)
        self.assertEqual(config.port, 9100)
        self.assertTrue(config.quiet)
        self.assertEqual(config.model, "env-model")

    def test_env_layer_coerces_paths(self):
        config = A.AgentConfig().with_env({"AGENT_THEME": "/tmp/theme.json"})
        self.assertEqual(config.theme, Path("/tmp/theme.json"))

    def test_to_dict_redacts_secrets(self):
        config = A.AgentConfig(api_key="sk-abcdefghijklmnop")
        self.assertEqual(config.to_dict()["api_key"], "sk-abcde...mnop")
        self.assertEqual(config.to_dict(redact=False)["api_key"], "sk-abcdefghijklmnop")

    def test_to_dict_redacts_role_secrets(self):
        config = A.AgentConfig(vision_api_key="sk-abcdefghijklmnop")
        self.assertEqual(config.to_dict()["vision_api_key"], "sk-abcde...mnop")

    def test_mask_secret(self):
        self.assertEqual(A.mask_secret("short"), "*****")
        self.assertEqual(A.mask_secret(""), "")


class ModelSpecTest(unittest.TestCase):
    def test_leader_spec_uses_the_primary_settings(self):
        config = A.AgentConfig(model="m", api_url="http://url", api_key="k")
        spec = config.model_spec()
        self.assertEqual((spec.role, spec.name, spec.api_url, spec.api_key),
                         ("leader", "m", "http://url", "k"))

    def test_vision_is_unset_by_default(self):
        self.assertIsNone(A.AgentConfig().model_spec(A.VISION_ROLE))

    def test_vision_inherits_the_leader_endpoint(self):
        config = A.AgentConfig(api_url="http://url", api_key="k", vision_model="v")
        spec = config.model_spec(A.VISION_ROLE)
        self.assertEqual((spec.name, spec.api_url, spec.api_key), ("v", "http://url", "k"))

    def test_vision_may_use_its_own_endpoint(self):
        config = A.AgentConfig(
            vision_model="v", vision_api_url="http://other", vision_api_key="k2"
        )
        spec = config.model_spec(A.VISION_ROLE)
        self.assertEqual((spec.api_url, spec.api_key), ("http://other", "k2"))

    def test_vision_model_appears_in_the_banner(self):
        keys = [k for k, _ in A.AgentConfig(vision_model="v").banner_items()]
        self.assertIn("Vision Model", keys)
        self.assertNotIn("Vision Model", [k for k, _ in A.AgentConfig().banner_items()])


class ModelPoolTest(unittest.TestCase):
    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.calls = []
            self.chat = self

        @property
        def completions(self):
            return self

        async def create(self, **kwargs):
            self.calls.append(kwargs)
            message = type("M", (), {"content": " a cat "})()
            return type("R", (), {"choices": [type("C", (), {"message": message})()]})()

    class Pool(A.ModelPool):
        def __init__(self):
            super().__init__()
            self.built = 0

        def client(self, spec):
            client = self._clients.get(spec.endpoint)
            if client is None:
                self.built += 1
                client = ModelPoolTest.FakeClient(base_url=spec.api_url)
                self._clients[spec.endpoint] = client
            return client

    def setUp(self):
        self.pool = self.Pool()

    def test_one_client_per_endpoint(self):
        leader = A.ModelSpec("leader", "m", "http://url", "k")
        vision = A.ModelSpec("vision", "v", "http://url", "k")
        other = A.ModelSpec("vision", "v", "http://other", "k")
        self.assertIs(self.pool.client(leader), self.pool.client(vision))
        self.assertIsNot(self.pool.client(leader), self.pool.client(other))
        self.assertEqual(self.pool.built, 2)

    def test_describe_image_sends_the_data_url_and_returns_text(self):
        spec = A.ModelSpec("vision", "v", "http://url", "k")
        answer = run(self.pool.describe_image(spec, "data:image/png;base64,AA", "what?"))
        self.assertEqual(answer, "a cat")
        payload = self.pool.client(spec).calls[0]
        self.assertEqual(payload["model"], "v")
        content = payload["messages"][1]["content"]
        self.assertEqual(content[0]["text"], "what?")
        self.assertEqual(content[1]["image_url"]["url"], "data:image/png;base64,AA")

    def test_clear_drops_cached_clients(self):
        spec = A.ModelSpec("leader", "m", "http://url", "k")
        first = self.pool.client(spec)
        self.pool.clear()
        self.assertIsNot(first, self.pool.client(spec))


class RateLimitError(Exception):
    """Named the way a provider SDK names it, which is how it is classified."""


class BadRequestError(Exception):
    pass


class Status(Exception):
    """An error that only says what the transport said: a status code."""

    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.response = type("Response", (), {"status_code": status})()


def no_sleep():
    """Patch `asyncio.sleep` and collect the delays a retry would have waited."""
    delays: list[float] = []

    async def sleep(seconds):
        delays.append(seconds)

    return unittest.mock.patch("asyncio.sleep", sleep), delays


class ErrorTaxonomyTest(unittest.TestCase):
    """Every failure of a run is reported as one of a handful of kinds."""

    def test_provider_errors_classify_by_class_name(self):
        self.assertEqual(A.classify_error(RateLimitError("slow down")), "rate_limit")
        self.assertEqual(A.classify_error(BadRequestError("nope")), "invalid_request")

    def test_builtin_failures_classify(self):
        self.assertEqual(A.classify_error(TimeoutError()), "timeout")
        self.assertEqual(A.classify_error(ConnectionError()), "connection")
        self.assertEqual(A.classify_error(OSError("socket")), "connection")
        self.assertEqual(A.classify_error(asyncio.CancelledError()), "cancelled")

    def test_unknown_failures_are_internal(self):
        self.assertEqual(A.classify_error(ValueError("boom")), "internal")

    def test_a_status_code_classifies_an_unknown_class(self):
        self.assertEqual(A.classify_error(Status(429)), "rate_limit")
        self.assertEqual(A.classify_error(Status(503)), "server")
        self.assertEqual(A.classify_error(Status(504)), "timeout")
        self.assertEqual(A.classify_error(Status(401)), "auth")
        self.assertEqual(A.classify_error(Status(422)), "invalid_request")

    def test_a_model_error_keeps_its_kind(self):
        error = A.ModelError("stalled", A.ErrorKind.TIMEOUT, role="vision")
        self.assertEqual(A.classify_error(error), "timeout")
        self.assertEqual(error.role, "vision")

    def test_only_some_kinds_are_worth_another_attempt(self):
        self.assertIn(A.ErrorKind.RATE_LIMIT, A.ErrorKind.TRANSIENT)
        self.assertIn(A.ErrorKind.SERVER, A.ErrorKind.TRANSIENT)
        self.assertNotIn(A.ErrorKind.AUTH, A.ErrorKind.TRANSIENT)
        self.assertNotIn(A.ErrorKind.INTERNAL, A.ErrorKind.TRANSIENT)


class RetryPolicyTest(unittest.TestCase):
    """One timeout per attempt, bounded retries, exponential backoff, jitter."""

    def test_from_config_reads_the_settings(self):
        config = A.AgentConfig(
            request_timeout=5.0, retry_attempts=4, retry_backoff=2.0, retry_jitter=2.0
        )
        policy = A.RetryPolicy.from_config(config)
        self.assertEqual(policy.timeout, 5.0)
        self.assertEqual(policy.attempts, 4)
        self.assertEqual(policy.backoff, 2.0)
        self.assertEqual(policy.jitter, 1.0)

    def test_backoff_grows_is_capped_and_is_jittered(self):
        policy = A.RetryPolicy(backoff=1.0, max_backoff=4.0, jitter=0.5)
        for attempt, ceiling in ((1, 1.0), (2, 2.0), (3, 4.0), (9, 4.0)):
            delay = policy.delay(attempt)
            self.assertGreaterEqual(delay, ceiling * 0.5)
            self.assertLessEqual(delay, ceiling)

    def test_no_jitter_is_plain_exponential_backoff(self):
        policy = A.RetryPolicy(backoff=0.25, max_backoff=10.0, jitter=0.0)
        self.assertEqual([policy.delay(n) for n in (1, 2, 3)], [0.25, 0.5, 1.0])

    def test_a_successful_call_is_made_once(self):
        calls = []

        async def operation():
            calls.append(1)
            return "ok"

        policy = A.RetryPolicy(attempts=3, timeout=0)
        self.assertEqual(run(policy.call(operation)), "ok")
        self.assertEqual(len(calls), 1)

    def test_a_transient_failure_is_tried_again(self):
        errors = [Status(503), RateLimitError("slow down")]

        async def operation():
            if errors:
                raise errors.pop(0)
            return "ok"

        patch, delays = no_sleep()
        policy = A.RetryPolicy(attempts=3, timeout=0, backoff=1.0, jitter=0.0)
        with patch:
            self.assertEqual(run(policy.call(operation)), "ok")
        self.assertEqual(delays, [1.0, 2.0])

    def test_retries_are_bounded_and_end_as_a_model_error(self):
        async def operation():
            raise RateLimitError("slow down")

        patch, delays = no_sleep()
        policy = A.RetryPolicy(attempts=3, timeout=0, backoff=0.0)
        with patch, self.assertRaises(A.ModelError) as caught:
            run(policy.call(operation, role="vision"))
        self.assertEqual(caught.exception.kind, "rate_limit")
        self.assertEqual(caught.exception.attempts, 3)
        self.assertEqual(caught.exception.role, "vision")
        self.assertIn("slow down", str(caught.exception))
        self.assertEqual(len(delays), 2)

    def test_a_refusal_is_reported_without_a_second_attempt(self):
        calls = []

        async def operation():
            calls.append(1)
            raise Status(401)

        policy = A.RetryPolicy(attempts=3, timeout=0, backoff=0.0)
        with self.assertRaises(A.ModelError) as caught:
            run(policy.call(operation))
        self.assertEqual(caught.exception.kind, "auth")
        self.assertEqual(len(calls), 1)

    def test_an_unknown_failure_is_raised_unchanged(self):
        async def operation():
            raise ValueError("boom")

        policy = A.RetryPolicy(attempts=3, timeout=0, backoff=0.0)
        with self.assertRaises(ValueError) as caught:
            run(policy.call(operation))
        self.assertEqual(str(caught.exception), "boom")

    def test_an_attempt_is_bounded_by_the_timeout(self):
        async def operation():
            await asyncio.Event().wait()

        policy = A.RetryPolicy(attempts=1, timeout=0.01)
        with self.assertRaises(A.ModelError) as caught:
            run(policy.call(operation))
        self.assertEqual(caught.exception.kind, "timeout")

    def test_an_unbounded_call_is_left_to_time_itself_out(self):
        async def operation():
            await asyncio.sleep(0)
            return "ok"

        policy = A.RetryPolicy(attempts=1, timeout=0.01)
        self.assertEqual(run(policy.call(operation, bounded=False)), "ok")

    def test_a_call_that_cannot_be_resumed_is_not_tried_again(self):
        calls = []

        async def operation():
            calls.append(1)
            raise Status(503)

        policy = A.RetryPolicy(attempts=3, timeout=0, backoff=0.0)
        with self.assertRaises(A.ModelError):
            run(policy.call(operation, resumable=lambda: False))
        self.assertEqual(len(calls), 1)

    def test_cancellation_is_never_retried(self):
        calls = []

        async def operation():
            calls.append(1)
            raise asyncio.CancelledError()

        policy = A.RetryPolicy(attempts=3, timeout=0, backoff=0.0)
        with self.assertRaises(asyncio.CancelledError):
            run(policy.call(operation))
        self.assertEqual(len(calls), 1)

    def test_every_retry_is_reported(self):
        errors = [Status(503)]
        seen: list[dict] = []

        async def operation():
            if errors:
                raise errors.pop(0)
            return "ok"

        async def on_retry(info):
            seen.append(info)

        patch, _ = no_sleep()
        policy = A.RetryPolicy(attempts=2, timeout=0, backoff=0.0)
        with patch:
            run(policy.call(operation, role="leader", on_retry=on_retry))
        self.assertEqual(seen[0]["error_kind"], "server")
        self.assertEqual(seen[0]["attempt"], 1)
        self.assertEqual(seen[0]["attempts"], 2)
        self.assertEqual(seen[0]["role"], "leader")


class UsageTest(unittest.TestCase):
    """Tokens are counted wherever they are spent and priced at the end."""

    def test_either_naming_is_recorded(self):
        usage = A.Usage()
        usage.record({"prompt_tokens": 10, "completion_tokens": 4})
        usage.record(types.SimpleNamespace(input_tokens=5, output_tokens=1, requests=2))
        self.assertEqual(usage.requests, 3)
        self.assertEqual(usage.input_tokens, 15)
        self.assertEqual(usage.output_tokens, 5)
        self.assertEqual(usage.total_tokens, 20)

    def test_nothing_is_recorded_without_a_payload(self):
        self.assertEqual(A.Usage().record(None).requests, 0)

    def test_tokens_are_priced_per_million(self):
        usage = A.Usage(input_tokens=1_000_000, output_tokens=500_000)
        self.assertAlmostEqual(usage.price(3.0, 15.0), 10.5)
        self.assertAlmostEqual(usage.to_dict()["cost"], 10.5)

    def test_an_unpriced_run_still_reports_its_tokens(self):
        usage = A.Usage(input_tokens=7)
        usage.price()
        self.assertEqual(usage.to_dict()["cost"], 0.0)
        self.assertEqual(usage.to_dict()["input_tokens"], 7)

    def test_a_payload_is_never_counted_twice_on_one_sink(self):
        sink = A.Usage()
        A.record_usage({"prompt_tokens": 3}, sink, sink)
        self.assertEqual(sink.input_tokens, 3)
        self.assertEqual(sink.requests, 1)

    def test_usage_of_prefers_the_running_totals_of_the_sdk(self):
        streamed = types.SimpleNamespace(
            context_wrapper=types.SimpleNamespace(usage="totals"), raw_responses=[]
        )
        self.assertEqual(A.usage_of(streamed), "totals")

    def test_usage_of_falls_back_to_the_raw_responses(self):
        streamed = types.SimpleNamespace(
            context_wrapper=None,
            raw_responses=[
                types.SimpleNamespace(usage={"input_tokens": 2, "output_tokens": 1}),
                types.SimpleNamespace(usage={"prompt_tokens": 3, "completion_tokens": 4}),
            ],
        )
        self.assertEqual(
            A.usage_of(streamed),
            {"requests": 2, "input_tokens": 5, "output_tokens": 5},
        )

    def test_usage_of_reports_nothing_when_the_sdk_keeps_nothing(self):
        self.assertIsNone(A.usage_of(types.SimpleNamespace()))


class ModelPoolResilienceTest(unittest.TestCase):
    """Specialist calls are bounded, retried and accounted like the leader."""

    class FailingClient:
        def __init__(self, errors=(), usage=None):
            self.errors = list(errors)
            self.usage = usage
            self.calls = []
            self.chat = self

        @property
        def completions(self):
            return self

        async def create(self, **kwargs):
            self.calls.append(kwargs)
            if self.errors:
                raise self.errors.pop(0)
            message = type("M", (), {"content": "done"})()
            return type(
                "R",
                (),
                {"choices": [type("C", (), {"message": message})()], "usage": self.usage},
            )()

    def pool(self, client, **policy):
        pool = A.ModelPool(A.RetryPolicy(timeout=0, backoff=0.0, **policy))
        pool._clients[("http://url", "k")] = client
        return pool

    def test_a_transient_failure_is_tried_again(self):
        client = self.FailingClient(errors=[Status(503)])
        pool = self.pool(client, attempts=2)
        spec = A.ModelSpec("vision", "v", "http://url", "k")
        patch, _ = no_sleep()
        with patch:
            answer = run(pool.complete(spec, [{"role": "user", "content": "hi"}]))
        self.assertEqual(answer, "done")
        self.assertEqual(len(client.calls), 2)

    def test_an_exhausted_call_reports_the_role_and_the_kind(self):
        client = self.FailingClient(errors=[Status(503), Status(503)])
        pool = self.pool(client, attempts=2)
        spec = A.ModelSpec("vision", "v", "http://url", "k")
        patch, _ = no_sleep()
        with patch, self.assertRaises(A.ModelError) as caught:
            run(pool.complete(spec, []))
        self.assertEqual(caught.exception.kind, "server")
        self.assertEqual(caught.exception.role, "vision")

    def test_tokens_are_recorded_on_the_pool_and_the_caller(self):
        client = self.FailingClient(usage={"prompt_tokens": 9, "completion_tokens": 3})
        pool = self.pool(client, attempts=1)
        spec = A.ModelSpec("vision", "v", "http://url", "k")
        sink = A.Usage()
        run(pool.complete(spec, [], usage=sink))
        self.assertEqual(sink.input_tokens, 9)
        self.assertEqual(sink.output_tokens, 3)
        self.assertEqual(pool.usage.input_tokens, 9)
        self.assertEqual(pool.usage.requests, 1)

    def test_the_sdk_is_not_allowed_to_retry_behind_the_policy(self):
        pool = A.ModelPool(A.RetryPolicy(timeout=42.0))
        client = pool.client(A.ModelSpec("leader", "m", "http://url", "k"))
        self.assertEqual(client.max_retries, 0)
        self.assertEqual(client.timeout, 42.0)

    def test_an_endpoint_without_a_key_gets_a_placeholder(self):
        environ = dict(os.environ)
        environ.pop("OPENAI_API_KEY", None)
        with unittest.mock.patch.dict(os.environ, environ, clear=True):
            pool = A.ModelPool()
            client = pool.client(A.ModelSpec("leader", "m", "http://url", ""))
            self.assertEqual(client.api_key, "no-key")

    def test_a_keyless_endpoint_keeps_the_environment_fallback(self):
        environ = dict(os.environ)
        environ["OPENAI_API_KEY"] = "from-env"
        with unittest.mock.patch.dict(os.environ, environ, clear=True):
            pool = A.ModelPool()
            client = pool.client(A.ModelSpec("leader", "m", "http://url", ""))
            self.assertEqual(client.api_key, "from-env")

    def test_an_unbounded_policy_leaves_the_transport_alone(self):
        pool = A.ModelPool(A.RetryPolicy(timeout=0))
        client = pool.client(A.ModelSpec("leader", "m", "http://url", "k"))
        self.assertEqual(client.max_retries, 0)
        self.assertNotEqual(client.timeout, 0)

    def test_the_totals_survive_a_clear(self):
        client = self.FailingClient(usage={"prompt_tokens": 2})
        pool = self.pool(client, attempts=1)
        run(pool.complete(A.ModelSpec("vision", "v", "http://url", "k"), []))
        pool.clear()
        self.assertEqual(pool.usage.input_tokens, 2)


class EngineResilienceTest(unittest.TestCase):
    """A run bounds its model calls, classifies what failed and prices it."""

    class RetryEngine(A.Engine):
        """An engine whose stream fails a given number of times first."""

        errors: list = []
        partial: bool = False

        def build_sdk_agent(self, config, tools):
            return {}

        async def stream(self, sdk_agent, model_input, config, result):
            self.attempts = getattr(self, "attempts", 0) + 1
            if self.partial:
                result.blocks.append(A.Block(id="b0", kind="output"))
            if self.errors:
                raise self.errors.pop(0)
            result.usage.record({"prompt_tokens": 100, "completion_tokens": 10})
            result.output = "ok"
            return result

    def engine(self, errors=(), partial=False, **overrides):
        engine = self.RetryEngine(
            env=False, retry_backoff=0.0, request_timeout=0, **overrides
        )
        engine.errors = list(errors)
        engine.partial = partial
        self.addCleanup(engine.close)
        return engine

    def test_a_stalled_leader_is_tried_again_before_anything_is_shown(self):
        engine = self.engine(errors=[Status(503)])
        patch, _ = no_sleep()
        with patch:
            result = run(engine.run("hello"))
        self.assertTrue(result.ok)
        self.assertEqual(engine.attempts, 2)

    def test_a_stream_that_already_produced_output_is_not_replayed(self):
        engine = self.engine(errors=[Status(503)], partial=True)
        result = run(engine.run("hello"))
        self.assertFalse(result.ok)
        self.assertEqual(engine.attempts, 1)
        self.assertEqual(result.error_kind, "server")

    def test_a_failure_carries_its_kind(self):
        engine = self.engine(errors=[Status(401)])
        result = run(engine.run("hello"))
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, "auth")
        self.assertFalse(result.transient)
        self.assertIn("auth", result.error)

    def test_an_exhausted_retry_is_reported_as_transient(self):
        engine = self.engine(errors=[Status(503), Status(503), Status(503)])
        patch, _ = no_sleep()
        with patch:
            result = run(engine.run("hello"))
        self.assertEqual(result.error_kind, "server")
        self.assertTrue(result.transient)
        self.assertEqual(engine.attempts, 3)

    def test_a_defect_in_the_harness_is_not_disguised_as_a_provider_fault(self):
        engine = self.engine(errors=[ValueError("boom")])
        result = run(engine.run("hello"))
        self.assertEqual(result.error, "boom")
        self.assertEqual(result.error_kind, "internal")
        self.assertEqual(engine.attempts, 1)

    def test_every_retry_is_published(self):
        engine = self.engine(errors=[Status(503)])
        seen: list[A.Event] = []
        engine.events.on(A.EventType.MODEL_RETRY, seen.append)
        patch, _ = no_sleep()
        with patch:
            run(engine.run("hello"))
        self.assertEqual(seen[0].data["error_kind"], "server")
        self.assertEqual(seen[0].data["role"], A.LEADER_ROLE)

    def test_a_retry_is_counted_against_the_run(self):
        engine = self.engine(errors=[Status(503)])
        patch, _ = no_sleep()
        with patch:
            result = run(engine.run("hello"))
        self.assertEqual(result.usage.retries, 1)

    def test_the_end_of_a_run_carries_its_tokens_and_its_cost(self):
        engine = self.engine(cost_input=3.0, cost_output=15.0)
        ended: list[dict] = []
        engine.events.on(A.EventType.AGENT_END, lambda e: ended.append(e.data))
        result = run(engine.run("hello"))
        self.assertEqual(result.usage.total_tokens, 110)
        self.assertAlmostEqual(result.usage.cost, 100 * 3.0 / 1e6 + 10 * 15.0 / 1e6)
        self.assertEqual(ended[0]["usage"]["total_tokens"], 110)
        self.assertAlmostEqual(ended[0]["usage"]["cost"], result.usage.cost)
        self.assertIsNone(ended[0]["error_kind"])

    def test_a_specialist_called_by_a_tool_is_accounted_on_the_run(self):
        class Accounting(self.RetryEngine):
            async def stream(self, sdk_agent, model_input, config, result):
                usage = A.current_usage()
                usage.record({"prompt_tokens": 40, "completion_tokens": 2})
                result.output = "ok"
                return result

        engine = Accounting(env=False)
        self.addCleanup(engine.close)
        result = run(engine.run("hello"))
        self.assertEqual(result.usage.input_tokens, 40)

    def test_cancellation_is_reported_as_cancellation(self):
        class Stuck(self.RetryEngine):
            async def stream(self, sdk_agent, model_input, config, result):
                raise asyncio.CancelledError()

        engine = Stuck(env=False)
        self.addCleanup(engine.close)
        with self.assertRaises(asyncio.CancelledError):
            run(engine.run("hello"))


class StallGuardTest(unittest.TestCase):
    """The leader may go quiet, but not for longer than the run allows."""

    def setUp(self):
        self.engine = A.Engine(env=False)
        self.addCleanup(self.engine.close)

    async def stream_of(self, *events, delay=0.0):
        for event in events:
            if delay:
                await asyncio.sleep(delay)
            yield event

    def test_an_event_arrives_within_the_budget(self):
        events = self.stream_of("first", "second").__aiter__()
        self.assertEqual(run(self.engine.next_event(events, 1.0)), "first")

    def test_a_silent_provider_fails_the_attempt(self):
        events = self.stream_of("late", delay=5.0).__aiter__()
        with self.assertRaises(A.ModelError) as caught:
            run(self.engine.next_event(events, 0.01))
        self.assertEqual(caught.exception.kind, "timeout")
        self.assertEqual(caught.exception.role, A.LEADER_ROLE)

    def test_the_end_of_a_stream_is_not_a_failure(self):
        events = self.stream_of().__aiter__()
        with self.assertRaises(StopAsyncIteration):
            run(self.engine.next_event(events, 1.0))

    def test_a_budget_of_zero_waits_forever(self):
        events = self.stream_of("first").__aiter__()
        self.assertEqual(run(self.engine.next_event(events, 0.0)), "first")

    def test_a_running_tool_extends_the_budget(self):
        policy = A.RetryPolicy(timeout=30.0)
        config = A.AgentConfig(shell_timeout=120)
        self.assertEqual(self.engine.stall_timeout(policy, config, False), 30.0)
        self.assertEqual(self.engine.stall_timeout(policy, config, True), 150.0)

    def test_an_unbounded_policy_never_stalls(self):
        policy = A.RetryPolicy(timeout=0.0)
        self.assertEqual(self.engine.stall_timeout(policy, A.AgentConfig(), True), 0.0)

    def test_an_abandoned_stream_is_cancelled(self):
        calls = []
        A.Engine.cancel_stream(types.SimpleNamespace(cancel=lambda: calls.append(1)))
        A.Engine.cancel_stream(types.SimpleNamespace())
        self.assertEqual(calls, [1])

class InputResolutionTest(unittest.TestCase):
    def test_raw_text_is_returned_as_is(self):
        self.assertEqual(A.resolve_input("just text"), "just text")
        self.assertEqual(A.resolve_input(""), "")

    def test_file_path_is_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prompt.md"
            path.write_text("from file", encoding="utf-8")
            self.assertEqual(A.resolve_input(str(path)), "from file")

    def test_agent_assigns_resolved_text_to_config_input(self):
        agent = A.Agent(console=False, store=A.MemoryStore())
        self.addCleanup(agent.close)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prompt.md"
            path.write_text("story seed", encoding="utf-8")
            config = agent.resolve_config(input=str(path))
        self.assertEqual(config.input, "story seed")
        self.assertTrue(config.input_source.endswith("prompt.md"))


class EventBusTest(unittest.TestCase):
    def test_sync_and_async_subscribers_receive_events(self):
        bus = A.EventBus()
        seen: list[str] = []
        bus.on("a", lambda e: seen.append(f"sync:{e.data['v']}"))

        async def handler(event):
            seen.append(f"async:{event.data['v']}")

        bus.on("a", handler)
        bus.on(A.EventType.ALL, lambda e: seen.append(f"all:{e.type}"))
        run(bus.publish("a", v=1))
        self.assertEqual(seen, ["sync:1", "async:1", "all:a"])

    def test_unsubscribe_and_failing_subscriber(self):
        bus = A.EventBus()
        seen: list[str] = []
        off = bus.on("a", lambda e: seen.append("first"))
        bus.on("a", lambda e: (_ for _ in ()).throw(RuntimeError("boom")))
        bus.on("a", lambda e: seen.append("last"))
        off()
        run(bus.publish("a"))
        self.assertEqual(seen, ["last"])  # failure never breaks the chain


class PromptTest(unittest.TestCase):
    def setUp(self):
        self.prompts = A.PromptRenderer()

    def test_renders_config_input(self):
        config = A.AgentConfig(input="the seed")
        rendered = self.prompts.render("Use: {{ config.input }}", config)
        self.assertEqual(rendered, "Use: the seed")

    def test_renders_extras_and_context(self):
        config = A.AgentConfig(extras={"genre": "noir"})
        rendered = self.prompts.render("{{ config.genre }}/{{ tone }}", config, tone="dry")
        self.assertEqual(rendered, "noir/dry")

    def test_strict_undefined_raises(self):
        from jinja2 import UndefinedError

        with self.assertRaises(UndefinedError):
            self.prompts.render("{{ nope }}", A.AgentConfig())

    def test_render_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.md"
            path.write_text("Hi {{ config.input }}", encoding="utf-8")
            self.assertEqual(
                self.prompts.render_file(path, A.AgentConfig(input="there")), "Hi there"
            )


class StorageTest(unittest.TestCase):
    def test_sqlite_store_roundtrip(self):
        store = A.SqliteStore()
        self.addCleanup(store.close)
        store.set("ns", "k", {"a": 1})
        self.assertEqual(store.get("ns", "k"), {"a": 1})
        store.set("ns", "k", {"a": 2})  # upsert
        self.assertEqual(store.list("ns"), [("k", {"a": 2})])
        store.delete("ns", "k")
        self.assertIsNone(store.get("ns", "k"))
        self.assertEqual(store.list("ns"), [])

    def test_sqlite_store_persists_to_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "agent.db"
            store = A.SqliteStore(path)
            store.set("ns", "k", "v")
            store.close()
            reopened = A.SqliteStore(path)
            self.addCleanup(reopened.close)
            self.assertEqual(reopened.get("ns", "k"), "v")

    def test_stores_satisfy_the_protocol(self):
        self.assertIsInstance(A.MemoryStore(), A.Store)
        self.assertIsInstance(A.LruCache(2), A.Cache)

    def test_lru_cache_evicts_least_recently_used(self):
        cache = A.LruCache(maxsize=2)
        cache.set("a", 1)
        cache.set("b", 2)
        self.assertEqual(cache.get("a"), 1)  # refresh 'a'
        cache.set("c", 3)
        self.assertIsNone(cache.get("b"))
        self.assertEqual(cache.get("c"), 3)
        cache.delete("c")
        self.assertIsNone(cache.get("c"))
        cache.clear()
        self.assertEqual(len(cache), 0)


class SessionTest(unittest.TestCase):
    def setUp(self):
        self.store = A.MemoryStore()
        self.sessions = A.SessionManager(ttl=60, store=self.store, cache=A.LruCache(4))
        self.addCleanup(self.sessions.close_all)

    def test_create_isolates_a_workspace(self):
        first = self.sessions.create()
        second = self.sessions.create()
        self.assertTrue(first.workspace.is_dir())
        self.assertNotEqual(first.workspace, second.workspace)
        self.assertEqual(self.store.get("sessions", first.id)["id"], first.id)

    def test_close_leaves_no_trace(self):
        session = self.sessions.create()
        (session.workspace / "note.txt").write_text("x", encoding="utf-8")
        self.assertTrue(self.sessions.close(session.id))
        self.assertFalse(session.workspace.exists())
        self.assertIsNone(self.store.get("sessions", session.id))
        self.assertFalse(self.sessions.close(session.id))

    def test_expired_sessions_are_purged(self):
        session = self.sessions.create()
        session.expires_at = 0
        self.assertTrue(session.expired)
        self.assertEqual(self.sessions.purge_expired(), [session.id])
        self.assertIsNone(self.sessions.get(session.id))
        self.assertFalse(session.workspace.exists())

    def test_ensure_reuses_a_live_session(self):
        session = self.sessions.create()
        self.assertIs(self.sessions.ensure(session.id).id, session.id)
        self.assertNotEqual(self.sessions.ensure(None).id, session.id)

    def test_keep_workspace_preserves_the_directory(self):
        manager = A.SessionManager(ttl=60, keep_workspace=True)
        session = manager.create()
        self.addCleanup(lambda: __import__("shutil").rmtree(session.workspace, True))
        manager.close(session.id)
        self.assertTrue(session.workspace.exists())

    def test_access_renews_the_lease(self):
        session = self.sessions.create()
        session.expires_at = A.time.time() + 1
        renewed = self.sessions.get(session.id)
        self.assertGreater(renewed.expires_at, A.time.time() + 30)
        self.assertGreater(self.store.get("sessions", session.id)["expires_at"], 0)


class DurableSessionTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root, True))
        self.store = A.MemoryStore()

    def manager(self, **overrides):
        options = dict(
            root=self.root, ttl=60, store=self.store, prefix="agent", durable=True
        )
        options.update(overrides)
        manager = A.SessionManager(**options)
        self.addCleanup(lambda: manager.close_all(destroy=True))
        return manager

    def test_a_durable_shutdown_keeps_the_workspace_and_the_record(self):
        first = self.manager()
        session = first.create()
        first.close_all()
        self.assertTrue(session.workspace.is_dir())
        self.assertIsNotNone(self.store.get("sessions", session.id))

    def test_live_sessions_are_rehydrated_on_startup(self):
        first = self.manager()
        session = first.create(who="me")
        (session.workspace / "note.txt").write_text("x", encoding="utf-8")
        first.close_all()
        second = self.manager()
        restored = second.get(session.id)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.workspace, session.workspace)
        self.assertEqual(restored.meta, {"who": "me"})
        self.assertEqual([s.id for s in second.list()], [session.id])

    def test_expired_and_vanished_records_are_reconciled_away(self):
        first = self.manager()
        stale = first.create()
        stale.expires_at = 0
        first.update(stale)
        vanished = first.create()
        __import__("shutil").rmtree(vanished.workspace)
        first.close_all()
        second = self.manager()
        self.assertEqual(second.list(), [])
        self.assertIsNone(self.store.get("sessions", stale.id))
        self.assertIsNone(self.store.get("sessions", vanished.id))
        self.assertFalse(stale.workspace.exists())

    def test_unreadable_records_are_dropped(self):
        self.store.set("sessions", "broken", {"nope": True})
        self.assertEqual(self.manager().list(), [])
        self.assertIsNone(self.store.get("sessions", "broken"))

    def test_directories_without_an_owner_are_reaped(self):
        manager = self.manager()
        session = manager.create()
        orphan = self.root / "agent-orphan-1"
        orphan.mkdir()
        foreign = self.root / "other-tool-1"
        foreign.mkdir()
        self.assertEqual(manager.orphans(), [orphan])
        self.assertEqual(manager.reap_orphans(), [orphan])
        self.assertFalse(orphan.exists())
        self.assertTrue(foreign.is_dir())
        self.assertTrue(session.workspace.is_dir())

    def test_kept_workspaces_are_never_reaped(self):
        manager = self.manager(keep_workspace=True)
        orphan = self.root / "agent-orphan-1"
        orphan.mkdir()
        self.assertEqual(manager.reap_orphans(), [])
        self.assertTrue(orphan.is_dir())

    def test_sweep_expires_and_reaps(self):
        manager = self.manager()
        session = manager.create()
        session.expires_at = 0
        orphan = self.root / "agent-orphan-1"
        orphan.mkdir()
        expired, reaped = manager.sweep()
        self.assertEqual(expired, [session.id])
        self.assertEqual(reaped, [orphan])
        self.assertFalse(session.workspace.exists())

    def test_the_sweeper_expires_on_a_timer(self):
        async def scenario():
            manager = self.manager(sweep_interval=0.01)
            session = manager.create()
            session.expires_at = 0
            manager.start_sweeper()
            self.assertIs(manager.start_sweeper(), manager._sweeper)
            for _ in range(200):
                await asyncio.sleep(0.01)
                if not manager.list():
                    break
            await manager.stop_sweeper()
            return session

        session = run(scenario())
        self.assertFalse(session.workspace.exists())

    def test_the_sweeper_is_optional(self):
        async def scenario():
            manager = self.manager(sweep_interval=0)
            self.assertIsNone(manager.start_sweeper())
            await manager.stop_sweeper()

        run(scenario())


class MemoryTest(unittest.TestCase):
    def setUp(self):
        self.store = A.MemoryStore()
        self.memory = A.ConversationMemory(self.store, max_turns=4, max_chars=0)

    def exchange(self, asked, answered):
        return run(
            self.memory.remember("s1", [A.Turn("user", asked), A.Turn("assistant", answered)])
        )

    def test_remembered_turns_become_chat_messages(self):
        self.exchange("hello", "hi")
        self.assertEqual(
            self.memory.history("s1"),
            [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}],
        )

    def test_empty_turns_are_not_remembered(self):
        self.exchange("hello", "   ")
        self.assertEqual(len(self.memory.transcript("s1")), 1)

    def test_transcripts_survive_a_new_memory_over_the_same_store(self):
        self.exchange("hello", "hi")
        reloaded = A.ConversationMemory(self.store)
        self.assertEqual(reloaded.transcript("s1").messages(), self.memory.history("s1"))

    def test_replay_carries_the_block_kind_of_each_role(self):
        self.exchange("hello", "hi")
        self.assertEqual([t["kind"] for t in self.memory.replay("s1")], ["prompt", "output"])

    def test_oldest_turns_fall_out_of_the_window(self):
        self.exchange("one", "1")
        self.exchange("two", "2")
        self.exchange("three", "3")
        self.assertEqual(
            [t.text for t in self.memory.transcript("s1").turns], ["two", "2", "three", "3"]
        )

    def test_a_character_budget_also_trims(self):
        memory = A.ConversationMemory(self.store, max_turns=0, max_chars=6)
        run(memory.remember("s2", [A.Turn("user", "aaaa"), A.Turn("assistant", "bbbb")]))
        self.assertEqual([t.text for t in memory.transcript("s2").turns], ["bbbb"])

    def test_the_newest_turn_always_survives(self):
        memory = A.ConversationMemory(self.store, max_turns=0, max_chars=1)
        run(memory.remember("s3", [A.Turn("user", "a long question")]))
        self.assertEqual(len(memory.transcript("s3")), 1)

    def test_a_summarizer_replaces_what_falls_out(self):
        async def summarize(turns):
            return "gist: " + ",".join(t.text for t in turns)

        memory = A.ConversationMemory(self.store, max_turns=2, summarizer=summarize)
        run(memory.remember("s4", [A.Turn("user", "one"), A.Turn("assistant", "1")]))
        run(memory.remember("s4", [A.Turn("user", "two"), A.Turn("assistant", "2")]))
        turns = memory.transcript("s4").turns
        self.assertEqual(turns[0].role, "system")
        self.assertEqual(turns[0].text, "gist: one,1")
        self.assertEqual([t.text for t in turns[1:]], ["two", "2"])

    def test_a_failing_summarizer_only_costs_the_summary(self):
        async def summarize(_turns):
            raise RuntimeError("boom")

        memory = A.ConversationMemory(self.store, max_turns=2, summarizer=summarize)
        run(memory.remember("s5", [A.Turn("user", "one"), A.Turn("assistant", "1")]))
        run(memory.remember("s5", [A.Turn("user", "two"), A.Turn("assistant", "2")]))
        self.assertEqual([t.text for t in memory.transcript("s5").turns], ["two", "2"])

    def test_forget_erases_the_transcript(self):
        self.exchange("hello", "hi")
        self.memory.forget("s1")
        self.assertEqual(self.memory.history("s1"), [])
        self.assertIsNone(self.store.get(A.ConversationMemory.NAMESPACE, "s1"))

    def test_disabled_memory_remembers_nothing(self):
        memory = A.ConversationMemory(self.store, enabled=False)
        run(memory.remember("s6", [A.Turn("user", "hello")]))
        self.assertEqual(memory.history("s6"), [])
        self.assertEqual(memory.replay("s6"), [])
        self.assertIsNone(self.store.get(A.ConversationMemory.NAMESPACE, "s6"))


class WorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = A.Workspace(self.tmp.name, shell_timeout=10)

    def test_paths_are_scoped_to_the_root(self):
        self.assertEqual(self.ws.resolve("a/b.txt"), self.ws.root / "a" / "b.txt")
        for escape in ("../outside.txt", "/etc/passwd", "a/../../x", ""):
            with self.assertRaises(A.WorkspaceError):
                self.ws.resolve(escape)

    def test_symlinks_cannot_escape_the_root(self):
        (Path(self.ws.root) / "link").symlink_to(Path(self.tmp.name).parent)
        with self.assertRaises(A.WorkspaceError):
            self.ws.resolve("link/outside.txt")

    def test_quotes_are_stripped(self):
        self.assertEqual(self.ws.resolve("'a.txt'"), self.ws.root / "a.txt")

    def test_read_write_and_list(self):
        self.ws.write_text("dir/file.txt", "hello")
        self.assertEqual(self.ws.read_text("dir/file.txt"), "hello")
        self.assertEqual(self.ws.list_dir("."), ["dir/"])
        with self.assertRaises(A.WorkspaceError):
            self.ws.read_text("missing.txt")
        with self.assertRaises(A.WorkspaceError):
            self.ws.list_dir("missing")

    def test_read_data_url(self):
        (Path(self.ws.root) / "pixel.png").write_bytes(b"\x89PNG\r\n")
        url = self.ws.read_data_url("pixel.png")
        self.assertTrue(url.startswith("data:image/png;base64,"))
        self.assertEqual(base64.b64decode(url.split(",", 1)[1]), b"\x89PNG\r\n")

    def test_empty_file_is_rejected_for_data_url(self):
        (Path(self.ws.root) / "empty.png").write_bytes(b"")
        with self.assertRaises(A.WorkspaceError):
            self.ws.read_data_url("empty.png")

    def test_shell_runs_in_the_workspace(self):
        output = run(self.ws.run_shell("pwd"))
        self.assertIn(str(self.ws.root), output)

    def test_shell_reports_failures(self):
        self.assertIn("[exit code 3]", run(self.ws.run_shell("exit 3")))

    def test_shell_times_out(self):
        ws = A.Workspace(self.tmp.name, shell_timeout=1)
        self.assertIn("timed out", run(ws.run_shell("sleep 5")))

    def test_guess_mime(self):
        self.assertEqual(A.guess_mime("a.png"), "image/png")
        self.assertEqual(A.guess_mime("a.unknown"), "application/octet-stream")


class ToolRegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = A.Workspace(self.tmp.name)
        (Path(self.ws.root) / "a.png").write_bytes(b"\x89PNG")

    @staticmethod
    def invoke(tool, payload):
        from agents.tool_context import ToolContext

        context = ToolContext(
            context=None, tool_name=tool.name, tool_call_id="1", tool_arguments=payload
        )
        return run(tool.on_invoke_tool(context, payload))

    def tools(self, vision=None):
        built = A.ToolRegistry(shell=False).build(self.ws, vision=vision)
        return {tool.name: tool for tool in built}

    def test_leader_sees_images_when_no_vision_model_is_configured(self):
        tools = self.tools()
        self.assertIn("view_image", tools)
        self.assertNotIn("describe_image", tools)
        self.assertTrue(
            self.invoke(tools["view_image"], '{"path": "a.png"}').startswith("data:image/png")
        )

    def test_vision_model_replaces_the_raw_image_tool(self):
        seen: list[tuple[str, str]] = []

        async def vision(data_url, question):
            seen.append((data_url, question))
            return "a cat"

        tools = self.tools(vision)
        self.assertIn("describe_image", tools)
        self.assertNotIn("view_image", tools)
        answer = self.invoke(tools["describe_image"], '{"path": "a.png", "question": "q"}')
        self.assertEqual(answer, "a cat")
        self.assertTrue(seen[0][0].startswith("data:image/png"))
        self.assertEqual(seen[0][1], "q")

    def test_vision_errors_are_reported_to_the_leader(self):
        async def vision(data_url, question):
            raise RuntimeError("upstream is down")

        tools = self.tools(vision)
        answer = self.invoke(tools["describe_image"], '{"path": "a.png"}')
        self.assertEqual(answer, "Error: the vision model failed: upstream is down")

    def test_registered_tools_are_appended(self):
        registry = A.ToolRegistry(defaults=False, shell=False)
        registry.register("mine", lambda ws: f"tool:{ws.root.name}")
        self.assertEqual(registry.build(self.ws), [f"tool:{self.ws.root.name}"])


class RepoSpecTest(unittest.TestCase):
    def test_https_ssh_and_shorthand_urls_resolve_to_the_same_repository(self):
        for url in (
            "https://github.com/owner/name.git",
            "git@github.com:owner/name.git",
            "owner/name",
        ):
            spec = A.RepoSpec(url=url)
            self.assertEqual(spec.slug, "owner/name", url)
            self.assertEqual(spec.name, "name", url)
            self.assertEqual(spec.api_url, "https://api.github.com", url)

    def test_enterprise_host_uses_its_own_api(self):
        spec = A.RepoSpec(url="https://git.acme.io/owner/name")
        self.assertEqual(spec.api_url, "https://git.acme.io/api/v3")

    def test_unknown_urls_are_rejected(self):
        with self.assertRaises(A.RepoError):
            _ = A.RepoSpec(url="not a repository").slug
        with self.assertRaises(A.RepoError):
            _ = A.RepoSpec().slug

    def test_filesystem_repositories_have_no_pull_request_api(self):
        spec = A.RepoSpec(url="/tmp/origin.git")
        self.assertTrue(spec.local)
        self.assertEqual(spec.name, "origin")
        self.assertEqual(spec.clone_url, "/tmp/origin.git")
        with self.assertRaises(A.RepoError):
            _ = spec.api_url

    def test_token_is_embedded_only_in_the_authenticated_url_and_masked(self):
        spec = A.RepoSpec(url="https://github.com/owner/name.git", token="ghp_secret")
        self.assertNotIn("ghp_secret", spec.clone_url)
        self.assertIn("x-access-token:ghp_secret@", spec.authenticated_url())
        self.assertEqual(spec.mask("fatal: ghp_secret"), "fatal: ***")

    def test_config_layers_into_a_spec(self):
        config = A.AgentConfig().with_env(
            {"AGENT_REPO_URL": "owner/name", "AGENT_REPO_BRANCH": "trunk"}
        )
        spec = A.RepoSpec.from_config(config)
        self.assertEqual((spec.url, spec.branch), ("owner/name", "trunk"))
        self.assertEqual(config.to_dict()["repo_token"], "")


def git(*args, cwd):
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        env={
            "PATH": __import__("os").environ.get("PATH", ""),
            "HOME": str(cwd),
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        },
    )


class RepoManagerTest(unittest.TestCase):
    """Clone, commit and push against a bare repository on disk."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.origin = self.root / "origin.git"
        seed = self.root / "seed"
        seed.mkdir()
        git("init", "--initial-branch", "main", cwd=seed)
        (seed / "README.md").write_text("seed", encoding="utf-8")
        git("add", "--all", cwd=seed)
        git("commit", "--message", "seed", cwd=seed)
        git("init", "--bare", "--initial-branch", "main", str(self.origin), cwd=self.root)
        git("remote", "add", "origin", str(self.origin), cwd=seed)
        git("push", "origin", "main", cwd=seed)
        self.manager = A.RepoManager(A.RepoSpec(url=str(self.origin)), timeout=60)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()

    def clone(self, session_id="abc123"):
        return run(self.manager.clone(self.workspace, session_id=session_id))

    def test_clone_checks_out_a_session_branch(self):
        checkout = self.clone()
        self.assertEqual(checkout.path, self.workspace / "origin")
        self.assertEqual(checkout.branch, "agent/abc123")
        self.assertEqual(checkout.base, "main")
        self.assertTrue((checkout.path / "README.md").is_file())
        self.assertEqual(
            run(self.manager.git("rev-parse", "--abbrev-ref", "HEAD", cwd=checkout.path)),
            "agent/abc123",
        )

    def test_clone_refuses_a_non_empty_target(self):
        self.clone()
        with self.assertRaises(A.RepoError):
            self.clone()

    def test_commit_and_push_reach_the_remote(self):
        checkout = self.clone()
        (checkout.path / "note.txt").write_text("work", encoding="utf-8")
        self.assertIn("add note", run(checkout.commit("add note")))
        run(checkout.push())
        branches = run(
            self.manager.git("ls-remote", "--heads", str(self.origin), cwd=self.workspace)
        )
        self.assertIn("agent/abc123", branches)

    def test_commit_without_changes_is_refused(self):
        checkout = self.clone()
        with self.assertRaises(A.RepoError):
            run(checkout.commit("nothing"))
        with self.assertRaises(A.RepoError):
            run(checkout.commit("   "))

    def test_status_reports_the_working_tree(self):
        checkout = self.clone()
        (checkout.path / "note.txt").write_text("work", encoding="utf-8")
        self.assertIn("note.txt", run(checkout.status()))

    def test_failing_git_commands_raise(self):
        with self.assertRaises(A.RepoError):
            run(self.manager.git("rev-parse", "--abbrev-ref", "HEAD", cwd=self.workspace))

    def test_pull_request_posts_to_the_forge(self):
        posted: list[tuple[str, dict]] = []

        class Forge(A.RepoManager):
            def _post(self, url, payload):
                posted.append((url, payload))
                return {"number": 7, "html_url": "https://example/pull/7", "state": "open"}

        manager = Forge(A.RepoSpec(url="owner/name", token="t"), timeout=5)
        pull = run(
            manager.open_pull_request(title="Work", body="b", head="agent/x", base="main")
        )
        self.assertEqual(pull["url"], "https://example/pull/7")
        self.assertEqual(posted[0][0], "https://api.github.com/repos/owner/name/pulls")
        self.assertEqual(posted[0][1]["head"], "agent/x")

    def test_pull_request_requires_a_token_a_title_and_a_base(self):
        manager = A.RepoManager(A.RepoSpec(url="owner/name"))
        for kwargs in (
            {"title": "", "head": "h", "base": "main"},
            {"title": "t", "head": "h", "base": "main"},
        ):
            with self.assertRaises(A.RepoError):
                run(manager.open_pull_request(**kwargs))
        manager.spec = A.RepoSpec(url="owner/name", token="t")
        with self.assertRaises(A.RepoError):
            run(manager.open_pull_request(title="t", head="h", base=""))


class AgentRepoTest(unittest.TestCase):
    """The session lifecycle around a repository backed workspace."""

    class LocalSpec(A.RepoSpec):
        """A filesystem repository that pretends to have a forge API."""

        @property
        def api_url(self) -> str:
            return "https://api.example"

    class Forge(A.RepoManager):
        def _post(self, url, payload):
            return {"number": 3, "html_url": "https://example/pull/3", "state": "open"}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.origin = self.root / "origin.git"
        seed = self.root / "seed"
        seed.mkdir()
        git("init", "--initial-branch", "main", cwd=seed)
        (seed / "README.md").write_text("seed", encoding="utf-8")
        git("add", "--all", cwd=seed)
        git("commit", "--message", "seed", cwd=seed)
        git("init", "--bare", "--initial-branch", "main", str(self.origin), cwd=self.root)
        git("remote", "add", "origin", str(self.origin), cwd=seed)
        git("push", "origin", "main", cwd=seed)
        self.agent = A.Agent(
            console=False,
            store=A.MemoryStore(),
            repos=self.Forge(timeout=60),
            repo_url=str(self.origin),
            repo_token="t",
        )
        self.agent.repos.spec = self.LocalSpec(url=str(self.origin), token="t")
        self.addCleanup(self.agent.close)

    def test_new_session_clones_the_default_repository(self):
        result = run(self.agent.commands.invoke("/new"))
        session = self.agent.sessions.get(result["session"]["id"])
        checkout = self.agent.checkout(session)
        self.assertIsNotNone(checkout)
        self.assertEqual(checkout.branch, f"agent/{session.id}")
        self.assertTrue((checkout.path / "README.md").is_file())
        self.assertEqual(session.meta["repo"]["base"], "main")

    def test_prepare_session_is_idempotent(self):
        session = self.agent.sessions.create()
        first = run(self.agent.prepare_session(session))
        second = run(self.agent.prepare_session(session))
        self.assertEqual(first.path, second.path)

    def test_a_clone_failure_leaves_the_session_usable(self):
        self.agent.repos.spec = A.RepoSpec(url="/nowhere/missing.git")
        session = self.agent.sessions.create()
        self.assertIsNone(run(self.agent.prepare_session(session)))
        self.assertIsNone(self.agent.checkout(session))

    def test_sessions_without_a_repository_have_no_git_tools(self):
        agent = A.Agent(console=False, store=A.MemoryStore())
        self.addCleanup(agent.close)
        session = agent.sessions.create()
        self.assertIsNone(run(agent.prepare_session(session)))
        names = {
            tool.name
            for tool in agent.build_tools(A.Workspace(session.workspace), agent.config)
        }
        self.assertNotIn("git_commit", names)

    def test_repository_sessions_expose_the_git_tools(self):
        session = self.agent.sessions.create()
        checkout = run(self.agent.prepare_session(session))
        tools = {
            tool.name: tool
            for tool in self.agent.build_tools(
                A.Workspace(session.workspace), self.agent.config, checkout
            )
        }
        self.assertLessEqual(
            {"git_status", "git_commit", "git_push", "open_pull_request", "publish_work"},
            set(tools),
        )
        (checkout.path / "note.txt").write_text("work", encoding="utf-8")
        answer = ToolRegistryTest.invoke(
            tools["publish_work"], json.dumps({"message": "add note"})
        )
        self.assertIn("https://example/pull/3", answer)

    def test_git_tool_errors_are_reported_to_the_leader(self):
        session = self.agent.sessions.create()
        checkout = run(self.agent.prepare_session(session))
        tools = {
            tool.name: tool
            for tool in self.agent.build_tools(
                A.Workspace(session.workspace), self.agent.config, checkout
            )
        }
        answer = ToolRegistryTest.invoke(tools["git_commit"], json.dumps({"message": "x"}))
        self.assertTrue(answer.startswith("Error:"), answer)

    def test_repo_commands_drive_the_checkout(self):
        created = run(self.agent.commands.invoke("/new"))
        session_id = created["session"]["id"]
        checkout = self.agent.checkout(self.agent.sessions.get(session_id))
        (checkout.path / "note.txt").write_text("work", encoding="utf-8")
        invoke = lambda text: run(
            self.agent.commands.invoke(text, session_id=session_id)
        )
        self.assertIn("note.txt", invoke("/status")["message"])
        self.assertTrue(invoke("/commit add note")["ok"])
        self.assertTrue(invoke("/push")["ok"])
        pull = invoke("/pr Add note\nA body")
        self.assertIn("https://example/pull/3", pull["message"])

    def test_repo_command_shows_and_clears_the_default_repository(self):
        self.assertIn(str(self.origin), run(self.agent.commands.invoke("/repo"))["message"])
        run(self.agent.commands.invoke("/repo owner/name"))
        self.assertEqual(self.agent.repos.spec.slug, "owner/name")
        run(self.agent.commands.invoke("/repo none"))
        self.assertFalse(self.agent.repos.configured)
        self.assertFalse(run(self.agent.commands.invoke("/clone"))["ok"])

    def test_commands_without_a_checkout_report_it(self):
        agent = A.Agent(console=False, store=A.MemoryStore())
        self.addCleanup(agent.close)
        session = agent.sessions.create()
        result = run(agent.commands.invoke("/push", session_id=session.id))
        self.assertFalse(result["ok"])
        self.assertIn("no git checkout", result["message"])


class CommandTest(unittest.TestCase):
    def setUp(self):
        self.commands = A.CommandRegistry()

    def test_parse(self):
        self.assertEqual(A.CommandRegistry.parse("/new  abc "), ("new", "abc"))
        self.assertEqual(A.CommandRegistry.parse("/HELP"), ("help", ""))
        self.assertIsNone(A.CommandRegistry.parse("hello"))
        self.assertIsNone(A.CommandRegistry.parse("/ nope"))

    def test_invoke_sync_and_async_handlers(self):
        self.commands.register("echo", "echo", lambda args, **_: {"ok": True, "args": args})

        async def slow(args, **_):
            return {"ok": True, "args": args.upper()}

        self.commands.register("loud", "loud", slow)
        self.assertEqual(run(self.commands.invoke("/echo hi"))["args"], "hi")
        self.assertEqual(run(self.commands.invoke("/loud hi"))["args"], "HI")
        self.assertIsNone(run(self.commands.invoke("plain text")))
        self.assertFalse(run(self.commands.invoke("/nope"))["ok"])

    def test_describe_is_sorted(self):
        self.commands.register("b", "second", lambda a, **_: None)
        self.commands.register("a", "first", lambda a, **_: None)
        self.assertEqual([c["name"] for c in self.commands.describe()], ["a", "b"])


class AgentCommandTest(unittest.TestCase):
    def setUp(self):
        self.agent = A.Agent(console=False, store=A.MemoryStore())
        self.addCleanup(self.agent.close)

    def test_default_commands_manage_sessions(self):
        created = run(self.agent.commands.invoke("/new"))
        session_id = created["session"]["id"]
        listed = run(self.agent.commands.invoke("/sessions"))
        self.assertIn(session_id, [s["id"] for s in listed["sessions"]])
        self.assertTrue(run(self.agent.commands.invoke(f"/use {session_id}"))["ok"])
        self.assertTrue(run(self.agent.commands.invoke(f"/end {session_id}"))["ok"])
        self.assertFalse(run(self.agent.commands.invoke(f"/use {session_id}"))["ok"])

    def test_model_command_updates_config(self):
        run(self.agent.commands.invoke("/model my-model"))
        self.assertEqual(self.agent.config.model, "my-model")

    def test_vision_command_sets_and_clears_the_vision_model(self):
        self.assertIn("(leader)", run(self.agent.commands.invoke("/vision"))["message"])
        run(self.agent.commands.invoke("/vision my-eyes"))
        self.assertEqual(self.agent.config.vision_model, "my-eyes")
        run(self.agent.commands.invoke("/vision none"))
        self.assertEqual(self.agent.config.vision_model, "")

    def test_build_vision_delegates_to_the_pool(self):
        calls: list[tuple] = []

        class Pool(A.ModelPool):
            async def describe_image(self, spec, data_url, question="", **kwargs):
                calls.append((spec, data_url, question, kwargs))
                return "described"

        agent = A.Agent(
            console=False, store=A.MemoryStore(), models=Pool(), vision_model="v"
        )
        self.addCleanup(agent.close)
        self.assertIsNone(agent.build_vision(A.AgentConfig()))
        delegate = agent.build_vision(agent.config)
        self.assertEqual(run(delegate("data:image/png;base64,AA", "q")), "described")
        spec, data_url, question, kwargs = calls[0]
        self.assertEqual((spec.name, question), ("v", "q"))
        self.assertEqual(kwargs["max_tokens"], agent.config.vision_max_tokens)

    def test_help_lists_commands(self):
        names = [c["name"] for c in run(self.agent.commands.invoke("/help"))["commands"]]
        self.assertIn("help", names)
        self.assertIn("new", names)


class EngineTest(unittest.TestCase):
    """The embeddable loop: no sessions, no storage, no memory, no console."""

    class FakeEngine(A.Engine):
        def build_sdk_agent(self, config, tools):
            self.built = {"config": config, "tools": tools}
            return self.built

        async def stream(self, sdk_agent, model_input, config, result):
            self.inputs = getattr(self, "inputs", [])
            self.inputs.append(model_input)
            result.output = A.prompt_of(model_input).upper()
            return result

    def test_engine_owns_no_batteries(self):
        engine = A.Engine(env=False)
        self.addCleanup(engine.close)
        for battery in ("store", "cache", "sessions", "memory", "repos", "commands"):
            self.assertFalse(hasattr(engine, battery), battery)
        self.assertIsNone(engine.renderer)

    def test_env_layer_can_be_skipped(self):
        import os

        os.environ["AGENT_MODEL"] = "env-model"
        self.addCleanup(os.environ.pop, "AGENT_MODEL", None)
        self.assertEqual(A.Engine(env=False).config.model, A.DEFAULT_MODEL)
        self.assertEqual(A.Engine().config.model, "env-model")

    def test_run_takes_the_model_input_verbatim(self):
        engine = self.FakeEngine(env=False)
        self.addCleanup(engine.close)
        seen: list[str] = []
        engine.events.on(A.EventType.ALL, lambda e: seen.append(e.type))
        messages = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "second"},
            {"role": "user", "content": "third"},
        ]
        result = run(engine.run(messages))
        self.assertTrue(result.ok)
        self.assertEqual(result.output, "THIRD")
        self.assertEqual(result.prompt, "third")
        self.assertEqual(engine.inputs[-1], messages)
        self.assertEqual(seen, ["agent.start", "agent.end"])

    def test_run_has_no_tools_without_a_workspace(self):
        engine = self.FakeEngine(env=False)
        self.addCleanup(engine.close)
        run(engine.run("hello"))
        self.assertEqual(engine.built["tools"], [])

    def test_a_workspace_brings_the_built_in_tools(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root, True))
        engine = self.FakeEngine(env=False, workspace=root)
        self.addCleanup(engine.close)
        run(engine.run("hello"))
        names = [tool.name for tool in engine.built["tools"]]
        self.assertIn("read_text_file", names)

    def test_tools_may_be_supplied_by_the_consumer(self):
        engine = self.FakeEngine(env=False)
        self.addCleanup(engine.close)
        run(engine.run("hello", tools=["mine"]))
        self.assertEqual(engine.built["tools"], ["mine"])

    def test_run_reports_errors_instead_of_raising(self):
        class Boom(self.FakeEngine):
            async def stream(self, sdk_agent, model_input, config, result):
                raise RuntimeError("boom")

        engine = Boom(env=False)
        self.addCleanup(engine.close)
        result = run(engine.run("hello"))
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "boom")

    def test_overrides_are_merged_per_run(self):
        engine = self.FakeEngine(env=False)
        self.addCleanup(engine.close)
        run(engine.run("hello", instructions="be brief"))
        self.assertEqual(engine.built["config"].instructions, "be brief")
        self.assertEqual(engine.config.instructions, A.DEFAULT_INSTRUCTIONS)

    def test_agent_is_an_engine(self):
        self.assertTrue(issubclass(A.Agent, A.Engine))


class StreamTest(unittest.TestCase):
    """The block stream itself, driven by a scripted SDK run."""

    class FakeStream:
        """What `Runner.run_streamed` returns: events, then an optional stall."""

        def __init__(self, events=(), final_output="", usage=None, stall=0.0):
            self.events = list(events)
            self.final_output = final_output
            self.context_wrapper = types.SimpleNamespace(usage=usage)
            self.raw_responses = []
            self.cancelled = False
            self.stall = stall

        def cancel(self):
            self.cancelled = True

        async def stream_events(self):
            for event in self.events:
                yield event
            if self.stall:
                await asyncio.sleep(self.stall)

    @staticmethod
    def delta(text, kind="response.output_text.delta"):
        from agents import RawResponsesStreamEvent

        return RawResponsesStreamEvent(
            data=types.SimpleNamespace(type=kind, delta=text)
        )

    @staticmethod
    def item(name, item):
        from agents import RunItemStreamEvent

        return RunItemStreamEvent(name=name, item=item)

    def drive(self, streamed, **overrides):
        """An engine, the result it will fill and a call that streams `streamed`."""
        import agents

        engine = A.Engine(env=False, **overrides)
        self.addCleanup(engine.close)
        config = engine.resolve_config()
        result = A.RunResult(session_id="s1")

        def stream():
            with unittest.mock.patch.object(
                agents.Runner, "run_streamed", lambda **_: streamed
            ):
                return run(engine.stream({}, "hello", config, result))

        return engine, result, stream

    def test_deltas_become_blocks_and_events(self):
        streamed = self.FakeStream(
            events=[
                self.delta("thinking", "response.reasoning_text.delta"),
                self.delta("hel"),
                self.delta("lo"),
            ],
            usage={"prompt_tokens": 12, "completion_tokens": 3},
        )
        engine, result, stream = self.drive(streamed)
        seen: list[str] = []
        engine.events.on(A.EventType.ALL, lambda e: seen.append(e.type))
        stream()
        self.assertEqual(result.output, "hello")
        self.assertEqual(result.text_of("reasoning"), "thinking")
        self.assertEqual([b.kind for b in result.blocks], ["reasoning", "output"])
        self.assertEqual(seen.count("block.start"), 2)
        self.assertEqual(seen.count("block.end"), 2)
        self.assertEqual(result.usage.input_tokens, 12)
        self.assertEqual(engine.models.usage.output_tokens, 3)

    def test_tool_calls_open_and_close_a_tool_block(self):
        call = types.SimpleNamespace(
            tool_name="read_text_file",
            raw_item=types.SimpleNamespace(call_id="c1", arguments='{"path": "a.txt"}'),
        )
        output = types.SimpleNamespace(output="contents", call_id="c1")
        streamed = self.FakeStream(
            events=[
                self.item("tool_called", call),
                self.item("tool_output", output),
                self.delta("done"),
            ],
            final_output="done",
        )
        _engine, result, stream = self.drive(streamed)
        stream()
        self.assertEqual([t.name for t in result.tools], ["read_text_file"])
        self.assertTrue(result.tools[0].done)
        self.assertEqual(result.tools[0].result, "contents")
        self.assertEqual(result.output, "done")

    def test_a_stalled_stream_fails_the_attempt_and_is_cancelled(self):
        streamed = self.FakeStream(stall=5.0)
        _engine, _result, stream = self.drive(streamed, request_timeout=0.01)
        with self.assertRaises(A.ModelError) as caught:
            stream()
        self.assertEqual(caught.exception.kind, "timeout")
        self.assertTrue(streamed.cancelled)

    def test_what_a_failed_attempt_spent_is_still_accounted(self):
        streamed = self.FakeStream(
            events=[self.delta("partial")],
            usage={"prompt_tokens": 8},
            stall=5.0,
        )
        _engine, result, stream = self.drive(streamed, request_timeout=0.01)
        with self.assertRaises(A.ModelError):
            stream()
        self.assertEqual(result.usage.input_tokens, 8)

    def test_a_run_survives_an_sdk_that_reports_no_usage(self):
        streamed = self.FakeStream(events=[self.delta("hi")])
        _engine, result, stream = self.drive(streamed)
        stream()
        self.assertEqual(result.output, "hi")
        self.assertEqual(result.usage.requests, 0)


class ToolCallTest(unittest.TestCase):
    """Tool calls are published with their name, arguments, outcome and time."""

    def setUp(self):
        self.engine = A.Engine(env=False)
        self.addCleanup(self.engine.close)

    @staticmethod
    def call_item(name="read_text_file", arguments='{"path": "a.txt"}', call_id="c1"):
        raw = types.SimpleNamespace(name=name, arguments=arguments, call_id=call_id)
        return types.SimpleNamespace(raw_item=raw, tool_name=name)

    @staticmethod
    def output_item(output="contents", call_id="c1"):
        return types.SimpleNamespace(
            raw_item={"call_id": call_id, "output": output},
            output=output,
            call_id=call_id,
        )

    def test_secrets_are_redacted_and_long_values_are_cut(self):
        redacted = A.redact({"path": "a.txt", "api_key": "sk-1", "n": 2, "big": "x" * 20}, 10)
        self.assertEqual(redacted["path"], "a.txt")
        self.assertEqual(redacted["api_key"], A.REDACTED)
        self.assertEqual(redacted["n"], 2)
        self.assertEqual(redacted["big"], "x" * 10 + "\u2026")
        self.assertEqual(A.redact([{"token": "t"}]), [{"token": A.REDACTED}])

    def test_arguments_are_parsed_from_the_json_of_the_model(self):
        self.assertEqual(self.engine.redact_arguments('{"path": "a"}'), {"path": "a"})
        self.assertEqual(self.engine.redact_arguments(""), {})
        self.assertEqual(self.engine.redact_arguments(None), {})
        self.assertEqual(self.engine.redact_arguments("not json"), "not json")

    def test_a_call_reports_its_signature_then_its_outcome(self):
        call = self.engine.tool_call_of(self.call_item(), "b0")
        self.assertEqual(call.name, "read_text_file")
        self.assertEqual(call.call_id, "c1")
        self.assertEqual(call.signature(), 'read_text_file(path="a.txt")')
        self.assertFalse(call.done)
        call.finish("contents")
        self.assertTrue(call.done)
        self.assertIn("-> ok in", call.report())
        self.assertIn("contents", call.report())
        self.assertGreaterEqual(call.duration, 0.0)
        self.assertEqual(call.to_dict()["name"], "read_text_file")

    def test_an_error_result_marks_the_call_as_failed(self):
        self.assertEqual(self.engine.tool_result_of(self.output_item("done")), ("done", True))
        text, ok = self.engine.tool_result_of(self.output_item("Error: nope"))
        self.assertEqual((text, ok), ("Error: nope", False))

    def test_an_output_is_matched_to_its_call(self):
        result = A.RunResult()
        first = A.ToolCall(id="b0", name="a", call_id="c1")
        second = A.ToolCall(id="b1", name="b", call_id="c2")
        result.tools.extend([first, second])
        self.assertIs(A.Engine.pending_tool(result, "c2"), second)
        self.assertIs(A.Engine.pending_tool(result, None), first)
        first.finish("x")
        second.finish("y")
        self.assertIsNone(A.Engine.pending_tool(result, "missing"))

    def test_the_stream_publishes_a_call_from_start_to_end(self):
        from agents import RunItemStreamEvent

        events = [
            RunItemStreamEvent(name="tool_called", item=self.call_item()),
            RunItemStreamEvent(name="tool_output", item=self.output_item()),
        ]

        class Streamed:
            final_output = "done"

            async def stream_events(self):
                for event in events:
                    yield event

        seen = []
        self.engine.events.on(A.EventType.ALL, lambda e: seen.append(e))
        result = A.RunResult(session_id="s1")
        with unittest.mock.patch("agents.Runner.run_streamed", return_value=Streamed()):
            run(self.engine.stream(None, "hi", self.engine.config, result))

        self.assertEqual([e.type for e in seen], ["tool.start", "tool.end"])
        start, end = (e.data for e in seen)
        self.assertEqual(start["name"], "read_text_file")
        self.assertEqual(start["arguments"], {"path": "a.txt"})
        self.assertEqual(start["kind"], "tool")
        self.assertEqual(end["result"], "contents")
        self.assertTrue(end["ok"])
        self.assertGreaterEqual(end["duration"], 0.0)
        self.assertEqual(end["id"], start["id"])
        self.assertEqual(len(result.tools), 1)
        self.assertEqual(result.text_of("tool"), result.tools[0].report())
        self.assertEqual(result.output, "done")


class AgentRunTest(unittest.TestCase):
    """Exercises the run pipeline with the SDK call stubbed out."""

    class FakeAgent(A.Agent):
        def build_sdk_agent(self, config, tools):
            return {"config": config, "tools": tools}

        async def stream(self, sdk_agent, model_input, config, result):
            self.inputs = getattr(self, "inputs", [])
            self.inputs.append(model_input)
            prompt = A.prompt_of(model_input)
            session_id = result.session_id
            block = A.Block(id="b0", kind="output", session_id=session_id)
            result.blocks.append(block)
            await self.events.publish(
                A.EventType.BLOCK_START, session_id=session_id, id=block.id, kind="output"
            )
            block.text = prompt.upper()
            await self.events.publish(
                A.EventType.BLOCK_DELTA,
                session_id=session_id,
                id=block.id,
                kind="output",
                text=block.text,
            )
            result.output = block.text
            return result

    def setUp(self):
        self.agent = self.FakeAgent(console=False, store=A.MemoryStore())
        self.addCleanup(self.agent.close)

    def test_run_renders_template_and_publishes_events(self):
        seen: list[str] = []
        self.agent.events.on(A.EventType.ALL, lambda e: seen.append(e.type))
        result = run(self.agent.run("Say: {{ config.input }}", input="hello"))
        self.assertTrue(result.ok)
        self.assertEqual(result.prompt, "Say: hello")
        self.assertEqual(result.output, "SAY: HELLO")
        self.assertEqual(result.text_of("output"), "SAY: HELLO")
        self.assertEqual(
            seen, ["agent.start", "block.start", "block.delta", "agent.end"]
        )

    def test_run_reuses_a_given_session(self):
        session = self.agent.sessions.create()
        result = run(self.agent.run("{{ config.input }}", session=session, input="x"))
        self.assertEqual(result.session_id, session.id)

    def test_run_reports_errors_instead_of_raising(self):
        result = run(self.agent.run("{{ missing }}", input="x"))
        self.assertFalse(result.ok)
        self.assertIn("missing", result.error)

    def test_attachments_are_appended_to_the_prompt(self):
        result = run(
            self.agent.run("Base", input="x", attachments=["attachments/a.png"])
        )
        self.assertIn("## Attached files", result.prompt)
        self.assertIn("attachments/a.png", result.prompt)

    def test_close_wipes_every_workspace(self):
        result = run(self.agent.run("{{ config.input }}", input="x"))
        workspace = self.agent.sessions.get(result.session_id).workspace
        self.agent.close()
        self.assertFalse(workspace.exists())

    def test_durable_sessions_survive_a_restart(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root, True))
        db = root / "agent.db"
        first = self.FakeAgent(
            console=False,
            store=A.SqliteStore(db),
            workspace_root=root,
            session_durable=True,
        )
        result = run(first.run("{{ config.input }}", input="hello"))
        workspace = first.sessions.get(result.session_id).workspace
        first.close()
        self.assertTrue(workspace.is_dir())
        second = self.FakeAgent(
            console=False,
            store=A.SqliteStore(db),
            workspace_root=root,
            session_durable=True,
        )
        self.addCleanup(lambda: second.sessions.close_all(destroy=True))
        restored = second.sessions.get(result.session_id)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.workspace, workspace)
        self.assertEqual(
            second.memory.history(result.session_id),
            [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "HELLO"},
            ],
        )

    def test_a_second_run_replays_the_conversation(self):
        session = self.agent.sessions.create()
        run(self.agent.run("{{ config.input }}", session=session, input="hello"))
        run(self.agent.run("{{ config.input }}", session=session, input="again"))
        self.assertEqual(self.agent.inputs[0], "hello")
        self.assertEqual(
            self.agent.inputs[1],
            [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "HELLO"},
                {"role": "user", "content": "again"},
            ],
        )

    def test_the_input_is_remembered_rather_than_the_rendered_prompt(self):
        session = self.agent.sessions.create()
        run(self.agent.run("Say: {{ config.input }}", session=session, input="hello"))
        self.assertEqual(
            self.agent.memory.history(session.id),
            [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "SAY: HELLO"},
            ],
        )

    def test_a_failed_run_is_not_remembered(self):
        result = run(self.agent.run("{{ missing }}", input="x"))
        self.assertEqual(self.agent.memory.history(result.session_id), [])

    def test_memory_can_be_disabled(self):
        agent = self.FakeAgent(console=False, store=A.MemoryStore(), memory_enabled=False)
        self.addCleanup(agent.close)
        session = agent.sessions.create()
        run(agent.run("{{ config.input }}", session=session, input="hello"))
        run(agent.run("{{ config.input }}", session=session, input="again"))
        self.assertEqual(agent.inputs, ["hello", "again"])
        self.assertEqual(agent.memory.history(session.id), [])

    def test_forget_clears_the_conversation_of_a_session(self):
        session = self.agent.sessions.create()
        run(self.agent.run("{{ config.input }}", session=session, input="hello"))
        result = run(self.agent.commands.invoke("/forget", session_id=session.id))
        self.assertTrue(result["ok"])
        self.assertEqual(self.agent.memory.history(session.id), [])

    def test_ending_a_session_forgets_it(self):
        session = self.agent.sessions.create()
        run(self.agent.run("{{ config.input }}", session=session, input="hello"))
        run(self.agent.commands.invoke("/end", session_id=session.id))
        self.assertEqual(self.agent.memory.history(session.id), [])

    def test_a_summarizer_is_only_built_when_asked_for(self):
        self.assertIsNone(self.agent.memory.summarizer)
        agent = self.FakeAgent(
            console=False, store=A.MemoryStore(), memory_summary=True, model="m"
        )
        self.addCleanup(agent.close)
        self.assertIsNotNone(agent.memory.summarizer)


class ConsoleRendererTest(unittest.TestCase):
    class Buffer:
        def __init__(self):
            self.text = ""

        def write(self, chunk):
            self.text += chunk

        def flush(self):
            pass

        def isatty(self):
            return False

    def setUp(self):
        self.buffer = self.Buffer()
        self.renderer = A.ConsoleRenderer(color=False, stream=self.buffer)

    def test_block_changes_insert_one_blank_line(self):
        self.renderer.emit("reasoning", "thinking")
        self.renderer.emit("output", "answer")
        self.assertEqual(self.buffer.text, "thinking\n\nanswer")

    def test_existing_newlines_are_not_doubled(self):
        self.renderer.emit("output", "line\n\n")
        self.renderer.emit("reasoning", "\n\nnext")
        self.assertEqual(self.buffer.text, "line\n\nnext")

    def test_empty_text_is_ignored(self):
        self.renderer.emit("output", "")
        self.assertEqual(self.buffer.text, "")

    def test_a_retry_is_announced(self):
        self.renderer.handle(
            A.Event(
                type=A.EventType.MODEL_RETRY,
                data={
                    "role": "leader",
                    "error_kind": "rate_limit",
                    "attempt": 1,
                    "attempts": 3,
                    "delay": 0.75,
                },
            )
        )
        self.assertEqual(
            self.buffer.text, "!! leader model rate_limit: retry 2/3 in 0.8s\n"
        )

    def test_color_styles_are_applied_when_enabled(self):
        renderer = A.ConsoleRenderer(color=True, stream=self.Buffer())
        self.assertTrue(renderer.style("reasoning", "x").endswith("\x1b[0m"))
        self.assertEqual(A.ConsoleRenderer(color=False, stream=self.buffer).style("x", "y"), "y")

    def test_banner_is_boxed(self):
        self.renderer.banner("title", [("Key", "value")])
        lines = self.buffer.text.splitlines()
        self.assertTrue(lines[0].startswith("+---"))
        self.assertIn("Key -> value", self.buffer.text)

    def test_renderer_subscribes_to_a_bus(self):
        bus = A.EventBus()
        self.renderer.attach(bus)
        run(bus.publish(A.EventType.BLOCK_DELTA, kind="output", text="hi"))
        self.assertEqual(self.buffer.text, "hi")

    def test_tool_calls_are_printed_with_their_outcome(self):
        bus = A.EventBus()
        self.renderer.attach(bus)
        run(bus.publish(A.EventType.TOOL_START, kind="tool", text='read(path="a")'))
        run(
            bus.publish(
                A.EventType.TOOL_END,
                kind="tool",
                name="read",
                ok=False,
                duration=0.002,
                result="Error: nope",
            )
        )
        self.assertIn('-> read(path="a")', self.buffer.text)
        self.assertIn("<- read: failed in 2 ms", self.buffer.text)
        self.assertIn("Error: nope", self.buffer.text)

    def test_truncate(self):
        self.assertEqual(A.truncate("abcdef", 10), "abcdef")
        self.assertEqual(A.truncate("abcdefghij", 6), "abc...")
        self.assertEqual(A.truncate("/a/very/long/path.txt", 10), "...ath.txt")


class ThemeTest(unittest.TestCase):
    def test_vscode_theme_maps_onto_css_variables(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "theme.json"
            path.write_text(
                json.dumps(
                    {
                        "type": "dark",
                        "colors": {
                            "editor.background": "#101010",
                            "editor.foreground": "#f0f0f0",
                            "unsupported.key": "#123456",
                        },
                    }
                ),
                encoding="utf-8",
            )
            theme = A.load_vscode_theme(path)
        self.assertEqual(theme["--bg"], "#101010")
        self.assertEqual(theme["--fg"], "#f0f0f0")
        self.assertEqual(theme["--theme-type"], "dark")
        self.assertNotIn("unsupported.key", theme)

    def test_missing_theme_is_empty(self):
        self.assertEqual(A.load_vscode_theme(None), {})

    def write_theme(self, **data) -> Path:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = tmp / "theme.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_a_variable_falls_back_to_the_next_color_the_theme_names(self):
        path = self.write_theme(
            colors={
                "editorGroupHeader.tabsBackground": "#202020",
                "editorWidget.border": "#303030",
            }
        )
        theme = A.load_vscode_theme(path)
        self.assertEqual(theme["--bg-soft"], "#202020")
        self.assertEqual(theme["--border"], "#303030")

    def test_a_variable_no_color_covers_is_left_to_the_stylesheet(self):
        theme = A.load_vscode_theme(self.write_theme(colors={"editor.background": "#101010"}))
        self.assertNotIn("--accent", theme)
        self.assertNotIn("--muted", theme)

    def test_theme_types_are_normalised(self):
        for raw, expected in (
            ("light", "light"),
            ("hcDark", "hc-dark"),
            ("hc", "hc-dark"),
            ("hcLight", "hc-light"),
            ("dark", "dark"),
            (None, "dark"),
            ("nonsense", "dark"),
        ):
            self.assertEqual(A.theme_type(raw), expected, raw)


class HubTest(unittest.TestCase):
    class FakeSocket:
        def __init__(self):
            self.sent: list[dict] = []

        async def send(self, raw):
            self.sent.append(json.loads(raw))

    def setUp(self):
        self.hub = A.Hub()
        self.socket = self.FakeSocket()
        self.connection = A.Connection(self.hub, self.socket)
        self.hub.connections[self.connection.id] = self.connection

    def test_channels_receive_broadcasts(self):
        self.hub.join(self.connection, "room")
        run(self.hub.broadcast("room", "tick", n=1))
        self.hub.leave(self.connection, "room")
        run(self.hub.broadcast("room", "tick", n=2))
        self.assertEqual(self.socket.sent, [{"type": "tick", "n": 1}])

    def test_dispatch_routes_to_handlers(self):
        seen = []
        self.hub.on("hi", lambda conn, msg: seen.append(msg["v"]))
        run(self.hub.dispatch(self.connection, {"type": "hi", "v": 7}))
        self.assertEqual(seen, [7])

    def test_dispatch_reports_unknown_types(self):
        run(self.hub.dispatch(self.connection, {"type": "nope"}))
        self.assertEqual(self.socket.sent[0]["type"], "error")


class WebServerTest(unittest.TestCase):
    def setUp(self):
        self.agent = A.Agent(console=False, store=A.MemoryStore())
        self.addCleanup(self.agent.close)
        self.server = A.WebServer(self.agent, template="{{ config.input }}")

    def test_rest_endpoints(self):
        status, body = self.server.rest("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        self.assertIn("model", json.loads(self.server.rest("/api/config")[1]))
        self.assertIsInstance(json.loads(self.server.rest("/api/commands")[1]), list)
        self.assertIsNone(self.server.rest("/api/unknown"))

    def test_event_payloads_drop_non_serializable_values(self):
        event = A.Event(
            type=A.EventType.BLOCK_DELTA, data={"text": "x", "config": A.AgentConfig()}
        )
        self.assertEqual(self.server.encode(event), {"text": "x"})

    def test_bind_moves_a_connection_between_channels(self):
        connection = A.Connection(self.server.hub, HubTest.FakeSocket())
        self.server.hub.connections[connection.id] = connection
        self.server.bind(connection, "one")
        self.server.bind(connection, "two")
        self.assertEqual(connection.channels, {A.WebServer.channel("two")})

    def test_hello_replays_the_conversation_of_the_session(self):
        session = self.agent.sessions.create()
        run(
            self.agent.memory.remember(
                session.id, [A.Turn("user", "hello"), A.Turn("assistant", "hi")]
            )
        )
        connection = A.Connection(self.server.hub, HubTest.FakeSocket())
        self.server.hub.connections[connection.id] = connection
        run(self.server.on_hello(connection, {"session": session.id}))
        hello = connection.ws.sent[-1]
        self.assertEqual(hello["session"]["id"], session.id)
        self.assertEqual(
            [(t["role"], t["text"]) for t in hello["history"]],
            [("user", "hello"), ("assistant", "hi")],
        )

    def test_attachments_are_stored_inside_the_session(self):
        session = self.agent.sessions.create()
        saved = self.server.store_attachments(
            session,
            [{"name": "../../evil name.png", "data": base64.b64encode(b"png").decode()}],
        )
        self.assertEqual(saved, ["attachments/evil_name.png"])
        self.assertEqual((session.workspace / saved[0]).read_bytes(), b"png")

    def test_oversized_attachments_are_rejected(self):
        session = self.agent.sessions.create()
        blob = base64.b64encode(b"x" * (A.MAX_ATTACHMENT_BYTES + 1)).decode()
        with self.assertRaises(ValueError):
            self.server.store_attachments(session, [{"name": "big.bin", "data": blob}])

    def test_invalid_attachment_encoding_is_rejected(self):
        session = self.agent.sessions.create()
        with self.assertRaises(ValueError):
            self.server.store_attachments(session, [{"name": "a.bin", "data": "@@@"}])

    def test_safe_name(self):
        self.assertEqual(A.safe_name("../../etc/passwd"), "passwd")
        self.assertEqual(A.safe_name(""), "file")
        self.assertEqual(A.safe_name("a b?c.txt"), "a_b_c.txt")

    def test_content_type_replaces_the_one_the_transport_set(self):
        from websockets.datastructures import Headers

        response = types.SimpleNamespace(headers=Headers())
        response.headers["Content-Type"] = "text/plain; charset=utf-8"
        A.WebServer.retype(response, "text/css; charset=utf-8")
        self.assertEqual(
            response.headers.get_all("Content-Type"), ["text/css; charset=utf-8"]
        )


class E2eCaptureTest(unittest.TestCase):
    """The scripted agent behind `agent_e2e.py`, without a browser."""

    def setUp(self):
        import agent_e2e as E

        self.E = E
        self.gate = E.Gate()
        self.agent = E.ScriptedAgent(
            console=False,
            env=False,
            config_file=False,
            gate=self.gate,
            pace=0.0,
            store=A.MemoryStore(),
        )
        self.addCleanup(self.agent.close)

    def test_chunks_rebuild_the_text_they_came_from(self):
        text = self.E.ANSWER_HEAD + self.E.ANSWER_TAIL
        self.assertEqual("".join(self.E.chunks(text)), text)

    def test_the_run_holds_until_the_capture_releases_it(self):
        seen = []
        self.agent.events.on(
            A.EventType.ALL,
            lambda event: seen.append((event.type, str(event.data.get("id", "")))),
        )

        async def drive():
            task = asyncio.ensure_future(self.agent.run("{{ config.input }}", input=self.E.PROMPT))
            while not self.gate.reached.is_set():
                await asyncio.sleep(0.01)
            held = list(seen)
            self.gate.release()
            return held, await task

        held, result = run(drive())
        types = [event for event, _id in held]
        ended = [id for event, id in held if event == A.EventType.BLOCK_END]
        self.assertIn(A.EventType.TOOL_END, types)
        # Reasoning and the tool call are behind the gate; the answer is not:
        # it is half written, which is what the middle frame photographs.
        self.assertTrue(any(id.endswith("-reasoning") for id in ended))
        self.assertFalse(any(id.endswith("-answer") for id in ended))
        self.assertTrue(result.ok)
        self.assertEqual(result.output, self.E.ANSWER_HEAD + self.E.ANSWER_TAIL)
        self.assertEqual([call.name for call in result.tools], [self.E.TOOL_NAME])

    def test_the_reasoning_is_streamed_before_the_answer(self):
        """The page can only show a reason block if the run publishes one."""
        seen = []
        self.agent.events.on(
            A.EventType.BLOCK_START,
            lambda event: seen.append(str(event.data.get("kind", ""))),
        )

        async def drive():
            task = asyncio.ensure_future(self.agent.run("{{ config.input }}", input=self.E.PROMPT))
            while not self.gate.reached.is_set():
                await asyncio.sleep(0.01)
            self.gate.release()
            return await task

        result = run(drive())
        self.assertEqual(seen, ["reasoning", "output"])
        self.assertEqual(result.text_of("reasoning"), self.E.REASONING)


class ConfigFileTest(unittest.TestCase):
    """The file layer: JSON or YAML, the cwd first and the module next to it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def write(self, name: str, text: str) -> Path:
        path = self.tmp / name
        path.write_text(textwrap.dedent(text).strip() + "\n", encoding="utf-8")
        return path

    def test_candidates_are_the_cwd_then_the_module_directory(self):
        names = [p.name for p in A.config_candidates(directories=[self.tmp])]
        self.assertEqual(names, ["agent.json", "agent.yaml", "agent.yml"])
        directories = A.config_directories()
        self.assertEqual(directories[0], Path.cwd())
        self.assertIn(A.ROOT, directories)

    def test_the_cwd_wins_over_the_module_directory(self):
        near, far = self.tmp / "near", self.tmp / "far"
        near.mkdir()
        far.mkdir()
        (far / "agent.yaml").write_text("model: far\n", encoding="utf-8")
        self.assertEqual(
            A.find_config_file(directories=[near, far]), far / "agent.yaml"
        )
        (near / "agent.yaml").write_text("model: near\n", encoding="utf-8")
        self.assertEqual(
            A.find_config_file(directories=[near, far]), near / "agent.yaml"
        )

    def test_a_missing_file_is_not_a_find(self):
        self.assertIsNone(A.find_config_file(directories=[self.tmp]))

    def test_json_and_yaml_read_the_same_way(self):
        js = self.write("agent.json", '{"model": "j", "port": 9001}')
        ya = self.write("agent.yaml", "model: y\nport: 9002")
        self.assertEqual(A.read_config_file(js)["model"], "j")
        self.assertEqual(A.read_config_file(ya)["port"], 9002)

    def test_values_are_coerced_to_the_field_types(self):
        path = self.write(
            "agent.yaml",
            """
            model: file-model
            port: 9100
            quiet: true
            theme: /tmp/theme.json
            retry_backoff: 2
            max_turns: "7"
            """,
        )
        config = A.AgentConfig().with_file(path)
        self.assertEqual(config.model, "file-model")
        self.assertEqual(config.port, 9100)
        self.assertIs(config.quiet, True)
        self.assertEqual(config.theme, Path("/tmp/theme.json"))
        self.assertEqual(config.retry_backoff, 2.0)
        self.assertEqual(config.max_turns, 7)

    def test_nested_groups_flatten_onto_field_names(self):
        path = self.write(
            "agent.yaml",
            """
            vision:
              model: eyes
              max_tokens: 33
            memory:
              enabled: false
            """,
        )
        config = A.AgentConfig().with_file(path)
        self.assertEqual(config.vision_model, "eyes")
        self.assertEqual(config.vision_max_tokens, 33)
        self.assertFalse(config.memory_enabled)

    def test_unknown_keys_and_nested_extras_land_in_extras(self):
        path = self.write(
            "agent.yaml",
            """
            genre: noir
            profile:
              tone: dry
            extras:
              mood: calm
            """,
        )
        config = A.AgentConfig().with_file(path)
        self.assertEqual(config.genre, "noir")
        self.assertEqual(config.profile, {"tone": "dry"})
        self.assertEqual(config.mood, "calm")

    def test_null_leaves_the_default_in_place(self):
        path = self.write("agent.yaml", "model: null")
        self.assertEqual(A.AgentConfig().with_file(path).model, A.DEFAULT_MODEL)

    def test_an_empty_file_is_harmless(self):
        self.assertEqual(A.read_config_file(self.write("agent.yaml", "")), {})

    def test_a_file_that_is_not_a_mapping_is_rejected(self):
        path = self.write("agent.json", "[1, 2, 3]")
        with self.assertRaises(ValueError):
            A.read_config_file(path)

    def test_without_a_path_nothing_found_means_no_change(self):
        with unittest.mock.patch.object(A, "find_config_file", return_value=None):
            self.assertEqual(A.AgentConfig(model="m").with_file().model, "m")

    def test_the_engine_layers_file_then_environment_then_arguments(self):
        path = self.write("agent.yaml", "model: from-file\nport: 9100")
        engine = A.Engine(config_file=path, env=False)
        self.assertEqual(engine.config_file, path)
        self.assertEqual(engine.config.model, "from-file")
        self.assertEqual(engine.config.port, 9100)
        with unittest.mock.patch.dict(
            "os.environ", {"AGENT_MODEL": "from-env"}, clear=False
        ):
            engine = A.Engine(config_file=path)
            self.assertEqual(engine.config.model, "from-env")
            self.assertEqual(engine.config.port, 9100)
            engine = A.Engine(config_file=path, model="from-argument")
            self.assertEqual(engine.config.model, "from-argument")

    def test_a_file_in_the_working_directory_is_found_on_its_own(self):
        self.write("agent.yaml", "model: from-the-cwd")
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(self.tmp)
        self.assertEqual(A.Engine(env=False).config.model, "from-the-cwd")
        self.assertIsNone(A.Engine(config_file=False, env=False).config_file)
        self.assertEqual(
            A.Engine(config_file=False, env=False).config.model, A.DEFAULT_MODEL
        )

    def test_a_named_file_that_does_not_exist_is_an_error(self):
        with self.assertRaises(FileNotFoundError):
            A.Engine(config_file=self.tmp / "nowhere.yaml")

    def test_an_agent_reads_the_same_layer(self):
        path = self.write("agent.yaml", "name: scribe\nmemory_max_turns: 5")
        agent = A.Agent(console=False, store=A.MemoryStore(), config_file=path)
        self.addCleanup(agent.close)
        self.assertEqual(agent.config.name, "scribe")
        self.assertEqual(agent.memory.max_turns, 5)


class ReplTest(unittest.TestCase):
    """The terminal client, driven by an injected reader."""

    class ReplAgent(A.Agent):
        """An agent whose runs are recorded instead of performed."""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.asked: list[str] = []

        async def run(self, template, *, config=None, session=None, context=None, **overrides):
            self.asked.append(str(overrides.get("input", "")))
            result = A.RunResult(session_id=getattr(session, "id", ""))
            result.output = f"echo: {overrides.get('input', '')}"
            return result

    def setUp(self):
        self.out = io.StringIO()
        self.agent = self.ReplAgent(
            console=False,
            store=A.MemoryStore(),
            config_file=False,
            renderer=A.ConsoleRenderer(color=False, stream=self.out),
        )
        self.addCleanup(self.agent.close)

    def repl(self, lines, **kwargs):
        feed = iter(lines)
        return self.agent.repl(reader=lambda prompt: next(feed), **kwargs)

    def test_a_plain_line_is_a_prompt_and_exit_leaves(self):
        repl = self.repl(["hello", "/exit", "never"])
        self.assertEqual(run(repl.start()), 0)
        self.assertEqual(self.agent.asked, ["hello"])

    def test_end_of_input_leaves(self):
        repl = self.repl(["one", None])
        run(repl.start())
        self.assertEqual(self.agent.asked, ["one"])

    def test_blank_lines_are_ignored(self):
        repl = self.repl(["", "   ", None])
        run(repl.start())
        self.assertEqual(self.agent.asked, [])

    def test_an_opening_prompt_is_answered_first(self):
        repl = self.repl([None])
        run(repl.start(opening="from the command line"))
        self.assertEqual(self.agent.asked, ["from the command line"])

    def test_a_slash_line_is_a_command(self):
        repl = self.repl(["/model tiny", None])
        run(repl.start())
        self.assertEqual(self.agent.asked, [])
        self.assertEqual(self.agent.config.model, "tiny")
        self.assertIn("Model: tiny", self.out.getvalue())

    def test_help_lists_the_repl_commands_too(self):
        repl = self.repl(["/help", None])
        run(repl.start())
        self.assertIn("/exit", self.out.getvalue())
        self.assertIn("/publish", self.out.getvalue())

    def test_an_unknown_command_is_reported(self):
        repl = self.repl(["/nope", None])
        run(repl.start())
        self.assertIn("Unknown command: /nope", self.out.getvalue())

    def test_new_and_end_move_the_repl_between_sessions(self):
        repl = self.repl(["/new", "/end", None])
        first = self.agent.sessions.ensure(None)
        repl.session = first
        run(repl.start())
        self.assertIsNone(repl.session)
        self.assertNotEqual(repl.current().id, first.id)

    def test_the_banner_is_printed_once_not_per_run(self):
        repl = self.repl(["hello", None])
        run(repl.start())
        self.assertEqual(self.out.getvalue().count("(repl)"), 1)
        self.assertFalse(self.agent.renderer.banners)

    def test_the_banner_names_the_config_file_and_session(self):
        repl = self.repl([None])
        repl.session = self.agent.sessions.ensure(None)
        self.agent.config_file = Path("/somewhere/agent.yaml")
        labels = [label for label, _ in repl.banner_items()]
        self.assertIn("Config", labels)
        self.assertIn("Session", labels)
        self.assertNotIn("Input", labels)

    def test_a_failing_run_keeps_the_repl_open(self):
        async def boom(*_args, **_kwargs):
            raise RuntimeError("model is down")

        self.agent.run = boom
        repl = self.repl(["one", "two", None])
        run(repl.start())
        self.assertEqual(self.out.getvalue().count("model is down"), 2)

    def test_an_interrupt_cancels_the_run_and_stays_open(self):
        async def forever(*_args, **_kwargs):
            await asyncio.sleep(30)

        self.agent.run = forever

        async def drive():
            repl = self.repl(["slow", None])

            async def interrupt():
                while repl.task is None:
                    await asyncio.sleep(0.01)
                repl.interrupt()

            asyncio.get_running_loop().create_task(interrupt())
            return await repl.start()

        self.assertEqual(run(drive()), 0)
        self.assertIn("(cancelled)", self.out.getvalue())

    def test_cancelling_the_repl_itself_stops_it(self):
        async def forever(*_args, **_kwargs):
            await asyncio.sleep(30)

        self.agent.run = forever

        async def drive():
            repl = self.repl(["slow", None])
            task = asyncio.create_task(repl.start())
            while repl.task is None:
                await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertFalse(repl.running)

        run(drive())

    def test_an_idle_interrupt_only_prints_a_hint(self):
        repl = self.repl([None])
        repl.interrupt()
        self.assertIn("Ctrl-D to leave", self.out.getvalue())

    def test_the_agent_builds_its_own_repl(self):
        self.assertIsInstance(self.agent.repl(), A.Repl)


class CliTest(unittest.TestCase):
    def test_the_accepted_arguments(self):
        args = A.parse_args(["--input", "notes.md", "--serve"])
        self.assertEqual(args.input, "notes.md")
        self.assertTrue(args.serve)
        self.assertEqual(
            vars(A.parse_args([])),
            {"input": "", "repl": False, "serve": False, "config": ""},
        )
        self.assertTrue(A.parse_args(["--repl"]).repl)
        self.assertEqual(A.parse_args(["--config", "c.yaml"]).config, "c.yaml")
        with self.assertRaises(SystemExit):
            A.parse_args(["--unknown"])

    def test_no_arguments_open_the_repl(self):
        agent = A.Agent(console=False, store=A.MemoryStore(), config_file=False)
        opened: list[str] = []

        class FakeRepl:
            async def start(self, opening=""):
                opened.append(opening)
                return 0

        agent.repl = lambda **_: FakeRepl()
        self.assertEqual(run(agent.execute("T", A.parse_args([]))), 0)
        self.assertEqual(opened, [""])

    def test_an_input_argument_performs_one_run(self):
        agent = A.Agent(console=False, store=A.MemoryStore(), config_file=False)
        ran: list[str] = []

        async def fake_run(template, **overrides):
            ran.append(overrides.get("input", ""))
            return A.RunResult(session_id="s")

        agent.run = fake_run
        self.assertEqual(run(agent.execute("T", A.parse_args(["--input", "hi"]))), 0)
        self.assertEqual(ran, ["hi"])

    def test_template_resolution_prefers_the_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.md"
            path.write_text("from file", encoding="utf-8")
            os.environ["AGENT_TEMPLATE"] = str(path)
            self.addCleanup(os.environ.pop, "AGENT_TEMPLATE", None)
            self.assertEqual(A.load_template(), "from file")
            os.environ["AGENT_TEMPLATE"] = "raw {{ config.input }}"
            self.assertEqual(A.load_template(), "raw {{ config.input }}")

    def test_template_resolution_prefers_the_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.md"
            path.write_text("from config", encoding="utf-8")
            config = A.AgentConfig(template=str(path))
            self.assertEqual(A.load_template(config), "from config")
            self.assertEqual(
                A.load_template(A.AgentConfig(template="raw one")), "raw one"
            )

    def test_template_candidates_follow_the_config_directories(self):
        names = {p.name for p in A.template_candidates()}
        self.assertEqual(names, {"agent_prompt.md"})
        self.assertIn(A.ROOT / "agent_prompt.md", A.template_candidates())

    def test_the_passthrough_template_is_the_last_resort(self):
        with unittest.mock.patch.object(A, "template_candidates", return_value=[]):
            self.assertEqual(A.load_template(A.AgentConfig()), A.PASSTHROUGH_TEMPLATE)


if __name__ == "__main__":
    unittest.main()
