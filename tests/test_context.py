from app.context.composer import ContextComposer


def test_context_composer():
    composer = ContextComposer()
    res = composer.compose(["Fact 1", "Fact 2"])
    assert "- Fact 1" in res
