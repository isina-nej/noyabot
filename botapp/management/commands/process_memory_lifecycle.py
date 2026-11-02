from django.core.management.base import BaseCommand

from botapp.memory import expire_memories
from botapp.memory.dialogue import prune_expired_dialogue


class Command(BaseCommand):
    help = "Expire non-critical memory items according to their configured TTL."

    def handle(self, *args, **options):
        count = expire_memories()
        dialogue_count = prune_expired_dialogue()
        self.stdout.write(
            self.style.SUCCESS(
                f"Expired {count} memory item(s) and {dialogue_count} dialogue turn(s)."
            )
        )
