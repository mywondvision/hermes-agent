# Выдержка из документации Codex о профилях разрешений (прочитано 2026-11-14)

Источник: https://learn.chatgpt.com/docs/permissions (раздел в начале страницы и «Define and select a profile»).

- «Permission profiles do not compose with the older sandbox settings. Configure either `default_permissions` and
  `[permissions]`, or `sandbox_mode` / `sandbox_workspace_write`, but not both. If `sandbox_mode` appears in any
  loaded config file, you pass `--sandbox`, or the selected config profile sets `sandbox_mode`, Codex uses those
  older sandbox settings instead of `default_permissions`.»
- «Create a named profile under `[permissions.<name>]`, then set the top-level `default_permissions` key to that
  profile name or to one of the built-ins.»
- «Local permission profiles are supported on macOS, Linux, WSL, and native Windows.»

Вывод для пилота: `codex exec` должен получить профиль через `default_permissions`, только если ни в одном
загруженном конфиге нет `sandbox_mode` и не передан `--sandbox`. На этом ПК для `codex exec` не проверено.
