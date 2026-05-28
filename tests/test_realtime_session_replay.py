from protean.executor import ExecutorEvent, ExecutorEventType
from protean.realtime.session import TeachSession
from protean.skills.schema import Skill, Step


class DummyDependency:
    pass


class FakeClient:
    def __init__(self) -> None:
        self.notifications: list[str] = []

    async def send_notification(self, text: str) -> None:
        self.notifications.append(text)


class FakeExecutor:
    def __init__(self) -> None:
        self.started: list[tuple[str, str]] = []

    async def start_task(
        self, instruction: str, context: str = "", images: list | None = None,
    ) -> None:
        self.started.append((instruction, context))

    async def send_message(self, message: str) -> None:
        self.started.append((message, ""))

    async def get_events(self):
        yield ExecutorEvent(type=ExecutorEventType.MESSAGE, message="done")
        yield ExecutorEvent(type=ExecutorEventType.DONE, message="done")

    async def interrupt(self) -> None:
        return None

    async def close(self) -> None:
        return None


async def test_replay_uses_latest_finalized_skill_when_draft_is_empty(monkeypatch):
    session = TeachSession(
        realtime_client=DummyDependency(),
        platform=DummyDependency(),
        skill_llm=DummyDependency(),
        executor=DummyDependency(),
        accept_call_id="test-call",
    )
    session._remember_finalized_skill(
        Skill(
            name="create-event",
            description="Create an event.",
            goal="Create an event.",
            steps=[
                Step(name="open-calendar", action="Open Calendar."),
                Step(name="create-event", action="Click New Event."),
            ],
        )
    )

    captured: list[str] = []

    async def fake_run_replay(skill: Skill) -> None:
        captured.extend(step.action for step in skill.steps)

    monkeypatch.setattr(session, "_run_replay", fake_run_replay)

    result = await session._tool_replay_skill({"name": "create-event"})
    await session._executor_task

    assert result.is_async is True
    assert captured == ["Open Calendar.", "Click New Event."]
    assert [step.intent for step in session._builder.steps] == ["open-calendar", "create-event"]


async def test_replay_named_finalized_skill_uses_skill_actions(monkeypatch):
    session = TeachSession(
        realtime_client=DummyDependency(),
        platform=DummyDependency(),
        skill_llm=DummyDependency(),
        executor=DummyDependency(),
        accept_call_id="test-call",
    )
    session._remember_finalized_skill(
        Skill(
            name="create-event",
            description="Create an event.",
            goal="Create an event.",
            steps=[
                Step(name="open-calendar", action="Open Calendar."),
            ],
        )
    )
    session._remember_finalized_skill(
        Skill(
            name="send-email",
            description="Send an email.",
            goal="Send an email.",
            steps=[
                Step(name="open-mail", action="Open Mail."),
                Step(name="compose", action="Click Compose."),
            ],
        )
    )

    captured: list[str] = []

    async def fake_run_replay(skill: Skill) -> None:
        captured.extend(step.action for step in skill.steps)

    monkeypatch.setattr(session, "_run_replay", fake_run_replay)

    result = await session._tool_replay_skill({"name": "create-event"})
    await session._executor_task

    assert result.is_async is True
    assert captured == ["Open Calendar."]


async def test_replay_latest_prefers_reinserted_skill_with_same_name(monkeypatch):
    session = TeachSession(
        realtime_client=DummyDependency(),
        platform=DummyDependency(),
        skill_llm=DummyDependency(),
        executor=DummyDependency(),
        accept_call_id="test-call",
    )
    session._remember_finalized_skill(
        Skill(
            name="create-event",
            description="Create an event.",
            goal="Create an event.",
            steps=[
                Step(name="old", action="Old action."),
            ],
        )
    )
    session._remember_finalized_skill(
        Skill(
            name="send-email",
            description="Send an email.",
            goal="Send an email.",
            steps=[
                Step(name="mail", action="Open Mail."),
            ],
        )
    )
    session._remember_finalized_skill(
        Skill(
            name="create-event",
            description="Create an event.",
            goal="Create an event.",
            steps=[
                Step(name="new", action="New action."),
            ],
        )
    )

    captured: list[str] = []

    async def fake_run_replay(skill: Skill) -> None:
        captured.extend(step.action for step in skill.steps)

    monkeypatch.setattr(session, "_run_replay", fake_run_replay)

    result = await session._tool_replay_skill({"name": "create-event"})
    await session._executor_task

    assert result.is_async is True
    assert captured == ["New action."]


async def test_replay_ignores_current_draft_and_uses_latest_finalized_skill(monkeypatch):
    session = TeachSession(
        realtime_client=DummyDependency(),
        platform=DummyDependency(),
        skill_llm=DummyDependency(),
        executor=DummyDependency(),
        accept_call_id="test-call",
    )
    session._builder.add_observed_step(
        intent="Open Calendar app",
        action="Open Calendar",
    )
    session._remember_finalized_skill(
        Skill(
            name="create-event",
            description="Create an event.",
            goal="Create an event.",
            steps=[
                Step(name="create-event", action="Click New Event."),
            ],
        )
    )

    captured: list[str] = []

    async def fake_run_replay(skill: Skill) -> None:
        captured.extend(step.action for step in skill.steps)

    monkeypatch.setattr(session, "_run_replay", fake_run_replay)

    result = await session._tool_replay_skill({"name": "create-event"})
    await session._executor_task

    assert result.is_async is True
    assert captured == ["Click New Event."]


async def test_replay_without_finalized_skill_returns_clear_message(monkeypatch, tmp_path):
    monkeypatch.setenv("PROTEAN_SKILLS_DIR", str(tmp_path / "skills"))

    session = TeachSession(
        realtime_client=DummyDependency(),
        platform=DummyDependency(),
        skill_llm=DummyDependency(),
        executor=DummyDependency(),
        accept_call_id="test-call",
    )

    result = await session._tool_replay_skill({})

    assert result.is_async is False
    assert result.message == "replay_skill requires a finalized skill name."


async def test_run_replay_updates_loaded_steps_without_duplication():
    session = TeachSession(
        realtime_client=FakeClient(),
        platform=DummyDependency(),
        skill_llm=DummyDependency(),
        executor=FakeExecutor(),
        accept_call_id="test-call",
    )
    steps = [
        Step(name="open-calendar", action="Open Calendar."),
        Step(name="create-event", action="Click New Event."),
    ]
    skill = Skill(
        name="test-skill",
        description="Test.",
        goal="Test.",
        steps=steps,
    )
    session._builder.load_steps(steps, source="skill")
    session._remember_finalized_skill(skill)

    await session._run_replay(skill)

    assert len(session._builder.steps) == 2
    assert [step.action for step in session._builder.steps] == [
        "Open Calendar.",
        "Click New Event.",
    ]
