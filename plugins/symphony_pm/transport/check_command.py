"""An owner-approved check. Git-dependent checks require a separately qualified environment."""
from dataclasses import dataclass
import re

from .types import Refused


@dataclass(frozen=True)
class CheckCommand:
    name: str
    tool: str
    arguments: tuple
    requires_git: bool = False


DIFF_CHECK = CheckCommand("git-diff-check", "git", ("diff", "--check", "<source_sha>", "<candidate_sha>"), True)


def validated_commands(commands):
    commands = tuple(commands)
    names = set()
    for command in commands:
        if (not isinstance(command, CheckCommand) or not isinstance(command.name, str)
                or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,80}', command.name)
                or command.name in names or type(command.requires_git) is not bool
                or (command.tool not in ('python', 'ruff') and command != DIFF_CHECK) or type(command.arguments) is not tuple
                or not all(isinstance(arg, str) for arg in command.arguments)):
            raise Refused('CHECK_COMMAND_POLICY')
        names.add(command.name)
    return commands
