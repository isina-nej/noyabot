# Generated for incremental, lossless emotional-memory decay
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("botapp", "0035_telegram_user_profile_opt_in"),
    ]

    operations = [
        migrations.AddField(
            model_name="noyarelationshipstate",
            name="decay_fraction",
            field=models.FloatField(default=0.0),
        ),
    ]
