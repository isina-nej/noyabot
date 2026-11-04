from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("botapp", "0036_noya_relationship_decay_fraction"),
    ]

    operations = [
        migrations.CreateModel(
            name="BotInboxMessage",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("bot_id", models.BigIntegerField()),
                ("chat_id", models.BigIntegerField()),
                ("message_id", models.BigIntegerField()),
                ("chat_type", models.CharField(default="private", max_length=32)),
                ("chat_title", models.CharField(blank=True, default="", max_length=255)),
                ("thread_id", models.BigIntegerField(default=0)),
                ("sender_user_id", models.BigIntegerField(blank=True, null=True)),
                ("sender_name", models.CharField(blank=True, default="", max_length=255)),
                ("sender_username", models.CharField(blank=True, default="", max_length=64)),
                ("is_outgoing", models.BooleanField(default=False)),
                ("text", models.TextField(blank=True, default="")),
                ("caption", models.TextField(blank=True, default="")),
                ("content_type", models.CharField(default="text", max_length=32)),
                ("media_file_id", models.CharField(blank=True, default="", max_length=512)),
                ("reply_to_message_id", models.BigIntegerField(blank=True, null=True)),
                ("message_date", models.DateTimeField(blank=True, null=True)),
                ("archived_at", models.DateTimeField(auto_now_add=True)),
                ("edited_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={
                "ordering": ["-archived_at"],
                "indexes": [
                    models.Index(fields=["bot_id", "chat_id", "-message_id"], name="inbox_peer_recent_idx"),
                    models.Index(fields=["bot_id", "-archived_at"], name="inbox_bot_recent_idx"),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=["bot_id", "chat_id", "message_id"],
                        name="uniq_bot_inbox_message_identity",
                    ),
                ],
            },
        ),
    ]
