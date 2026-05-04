# Extending group3lib to a new device model

`group3lib` is designed so that adding DTM-152, DTM-333, HTM-121, or any other
Group3 device does **not** require editing the transport, protocol, or session
layers — only the command registry (if commands differ), the parser (if reply
shapes differ), and a new model class.

## Step by step

1. **Read the model's manual.** Identify every command that differs from
   DTM-151-S — different syntax, different reply format, different range
   indexing, different error strings. Commands that are byte-for-byte identical
   can be reused as-is.
2. **Extend `protocol/commands.py`.** Add a new section comment block for the
   model and define its constants/builders there:
   ```python
   # -------------------------------------------------------------------------
   # DTM-333 commands (§4.5.2 of DTM-333 manual v2.3)
   # -------------------------------------------------------------------------
   ```
   If a command name collides with DTM-151 but has different semantics, prefix
   or suffix the constant (e.g., `DTM333_IR`). Never reuse a name with a
   different meaning.
3. **Extend `protocol/parser.py` only if needed.** If the new model uses a new
   error string or a new reply format, add a parser function there. Do not
   parse in the model layer.
4. **Create `src/group3/models/<model>.py`.** Compose, don't inherit. The new
   class takes a `Group3Protocol` in its constructor and exposes model-specific
   methods. Even if 90% of the API matches DTM-151-S, write a separate class —
   do not subclass `DTM151Serial`, because once the API is published, user
   expectations diverge.
5. **Add golden-transcript tests** in `tests/test_<model>.py` covering every
   public method, including error replies that differ from DTM-151.
6. **Session layer** is usually untouched — G3CL framing is identical across
   Group3 devices. If a model really needs different addressing, subclass
   `G3CLSession` rather than patching the base.
7. **Update `README.md`** "Supported models" and retire any "Assumptions"
   items that the new model's manual resolves.

## Why compose instead of inherit

Every time we've seen a public SDK lean on inheritance to share device-model
code, the base class ends up riddled with `if self._model == "..."` branches
within a year. Per-model classes keep the public API intentional: a new-model
PR diffs in three places (`commands.py`, `models/<new>.py`, `tests/`) without
touching transport or session, and reviewers can check every byte against the
manual in one sitting.

## Anti-patterns

- `if isinstance(self, DTM151Serial)` anywhere in the codebase.
- Reusing an existing command constant with different semantics for a new model.
- Widening `Reading`, `Unit`, or the exceptions to accommodate a new model when
  adding new types would be clearer.
- Silent fallbacks ("if the reply doesn't match the new format, try the old
  one"). If the model's protocol is different, model it as different code.
