from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("botapp", "0034_noya_dialogue_turn"),
    ]

    operations = [
        migrations.AddField(model_name="telegramuser", name="profile_opt_in", field=models.BooleanField(default=False)),
        migrations.AddField(model_name="telegramuser", name="profile_first_name", field=models.CharField(blank=True, default="", max_length=128)),
        migrations.AddField(model_name="telegramuser", name="profile_username", field=models.CharField(blank=True, default="", max_length=64)),
        migrations.AddField(model_name="telegramuser", name="profile_bio", field=models.TextField(blank=True, default="")),
        migrations.AddField(model_name="telegramuser", name="profile_birthdate", field=models.CharField(blank=True, default="", max_length=10)),
        migrations.AddField(model_name="telegramuser", name="profile_photo_file_id", field=models.CharField(blank=True, default="", max_length=256)),
        migrations.AddField(model_name="telegramuser", name="profile_updated_at", field=models.DateTimeField(blank=True, null=True)),
    ]
