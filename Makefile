# Convenience wrappers. On Windows without make: run the `uv run ...` commands directly.
.PHONY: demo eval test watch

demo:
	uv run greenlight demo

eval:
	uv run greenlight eval

test:
	uv run pytest -q

watch:
	uv run greenlight watch --interval 20
