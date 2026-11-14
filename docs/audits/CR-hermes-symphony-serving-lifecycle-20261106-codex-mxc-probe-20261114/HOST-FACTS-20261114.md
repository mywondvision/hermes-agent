# Факты хоста для CR codex-mxc-probe (2026-11-14, только чтение)

```
Windows build 26200.9974 25H2 Core
CodexSandboxOffline enabled=True
CodexSandboxOnline enabled=True
wsl installed: False
codex: codex-cli 0.167.0 at D:\Codex\npm-global
  -c, --config <key=value>
  -P, --permission-profile <NAME>
  -C, --cd <DIR>
Microsoft-Hyper-V-All package present: True
Containers-DisposableClientVM package present: True
hypervisor present: False
```

Редакция `Core` = Windows 11 Home: Hyper-V и Windows Sandbox в ней официально не поддерживаются (документация Microsoft). Пакеты этих компонентов в хранилище есть, гипервизор не запущен; включение не проверялось.
