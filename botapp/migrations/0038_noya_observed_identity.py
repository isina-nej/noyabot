# Generated for Noya observed, scoped identity metadata.
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("botapp", "0037_bot_inbox_message"),
    ]

    operations = [
        migrations.CreateModel(
            name="NoyaObservedIdentity",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("chat_id", models.BigIntegerField(db_index=True)),
                ("user_id", models.BigIntegerField()),
                ("username", models.CharField(max_length=64, blank=True, default="")),
                ("first_name", models.CharField(max_length=128, blank=True, default="")),
                ("last_name", models.CharField(max_length=128, blank=True, default="")),
                ("nickname", models.CharField(max_length=64, blank=True, default="")),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(fields=("chat_id", "user_id"), name="uniq_noya_observed_identity"),
                ],
            },
        ),
        migrations.AddIndex(
            model_name="messagesnapshot",
            index=models.Index(fields=["chat_id", "thread_id", "-message_id"], name="noya_context_scan_idx"),
        ),
    ]
