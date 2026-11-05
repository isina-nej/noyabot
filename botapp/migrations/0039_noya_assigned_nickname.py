# Chat-scoped group label is separate from a person's own nickname.
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("botapp", "0038_noya_observed_identity")]

    operations = [
        migrations.AddField(
            model_name="noyaobservedidentity",
            name="assigned_nickname",
            field=models.CharField(max_length=64, blank=True, default=""),
        ),
        migrations.AddField(
            model_name="noyaobservedidentity",
            name="assigned_nickname_by",
            field=models.BigIntegerField(null=True, blank=True),
        ),
    ]
