from django.db import migrations, models
import django.core.validators


class Migration(migrations.Migration):
    dependencies = [
        ("botapp", "0031_groupquota_unlimited_default"),
    ]

    operations = [
        migrations.CreateModel(
            name="NoyaRelationshipState",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("hurt_score", models.PositiveSmallIntegerField(default=0, validators=[django.core.validators.MinValueValidator(0), django.core.validators.MaxValueValidator(100)])),
                ("repair_progress", models.PositiveSmallIntegerField(default=0, validators=[django.core.validators.MinValueValidator(0), django.core.validators.MaxValueValidator(100)])),
                ("warmth", models.PositiveSmallIntegerField(default=55, validators=[django.core.validators.MinValueValidator(0), django.core.validators.MaxValueValidator(100)])),
                ("playfulness", models.PositiveSmallIntegerField(default=55, validators=[django.core.validators.MinValueValidator(0), django.core.validators.MaxValueValidator(100)])),
                ("trust_context", models.PositiveSmallIntegerField(default=50, validators=[django.core.validators.MinValueValidator(0), django.core.validators.MaxValueValidator(100)])),
                ("current_social_mood", models.CharField(default="neutral", max_length=32)),
                ("active_conflict_id", models.CharField(blank=True, default="", max_length=128)),
                ("processed_event_ids", models.JSONField(blank=True, default=list)),
                ("last_interaction_at", models.DateTimeField(blank=True, null=True)),
                ("last_negative_event_at", models.DateTimeField(blank=True, null=True)),
                ("last_reconciliation_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("conversation", models.ForeignKey(on_delete=models.deletion.CASCADE, related_name="noya_relationship_states", to="botapp.memoryconversation")),
                ("user", models.ForeignKey(on_delete=models.deletion.CASCADE, related_name="noya_relationship_states", to="botapp.telegramuser")),
            ],
            options={
                "constraints": [models.UniqueConstraint(fields=("user", "conversation"), name="unique_noya_relationship_user_conversation")],
                "indexes": [models.Index(fields=["user", "updated_at"], name="botapp_noya_user_id_2d833f_idx"), models.Index(fields=["conversation", "updated_at"], name="botapp_noya_convers_d10298_idx")],
            },
        ),
        migrations.CreateModel(
            name="NoyaRelationshipEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("event_id", models.CharField(max_length=160)),
                ("kind", models.CharField(max_length=64)),
                ("severity", models.FloatField(default=0)),
                ("confidence", models.FloatField(default=1)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("state", models.ForeignKey(on_delete=models.deletion.CASCADE, related_name="events", to="botapp.noyarelationshipstate")),
            ],
            options={
                "constraints": [models.UniqueConstraint(fields=("state", "event_id"), name="unique_noya_relationship_event")],
                "indexes": [models.Index(fields=["state", "created_at"], name="botapp_noya_state_i_f28f23_idx"), models.Index(fields=["created_at"], name="botapp_noya_created_0c6cba_idx")],
            },
        ),
    ]
