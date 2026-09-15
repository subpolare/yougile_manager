"""Explicit, repeatable setup of one configured employee's empty voice destination."""
from __future__ import annotations

import argparse
import asyncio

from app.config import Settings, VOICE_TASK_PROJECTS, load_employee_mappings
from app.personal_identity import PersonalIdentity
from app.yougile import YouGileClient


async def setup(employee: str) -> None:
    settings = Settings()
    identity = PersonalIdentity(None, load_employee_mappings(),
                                voice_task_employees=settings.voice_task_employees)
    project = VOICE_TASK_PROJECTS[employee]
    owners = [uid for uid, title in identity.voice_task_projects.items() if title == project]
    if len(owners) != 1:
        raise ValueError("Configure an unambiguous employee TG/YG identity before setup")
    async with YouGileClient(settings.yougile_api_key.get_secret_value(),
                            base_url=settings.yougile_base_url) as client:
        await client.ensure_voice_task_destination(project, owner_id=owners[0])
    print(f"Verified: {project} / Задачи из бота")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("employee", choices=VOICE_TASK_PROJECTS)
    args = parser.parse_args()
    # Never render raw SDK exceptions or request bodies from a setup failure.
    try:
        asyncio.run(setup(args.employee))
    except Exception as exc:
        raise SystemExit(f"Setup failed: {type(exc).__name__}") from None


if __name__ == "__main__":
    main()
