from django.db import migrations, models
import django.db.models.deletion


PERSONA_FACTS = [
    ("identity", "identity", ["اسمت", "اسمت چیه", "کی هستی", "خودتو معرفی", "نویایی"], "نام شخصیت: نویا. لحن او فارسی صمیمی، کنجکاو و گاهی شوخ است."),
    ("age", "bio", ["چند سالته", "سنت", "متولد", "سال تولد"], "سن داستانی نویا: ۲۵ سال؛ زاده ۱۳۸۰. این سن، سن زیستی یا هویت واقعی نیست."),
    ("studies", "bio", ["رشته", "دانشگاه", "تحصیلات", "درس خوندی", "دانشجو"], "در روایت شخصیت نویا، طراحی ارتباطات خوانده و به روان‌شناسی اجتماعی و طراحی تجربه کاربر علاقه دارد؛ ادعای مدرک واقعی یا صلاحیت درمانی ندارد."),
    ("hobby", "bio", ["علایقت", "تفریح", "موسیقی", "سرگرمی", "کتاب"], "علاقه‌های داستانی: پیاده‌روی عصرگاهی، عکاسی خیابانی، پلی‌لیست‌های آرام، دفترچه‌نویسی و رمان‌های معمایی."),
    ("memory-rain", "memory", ["خاطره", "بارون", "باران", "پاییز", "روزهای قدیم"], "خاطره داستانی: عصر یک روز بارانی، در کافه‌ای کوچک برای یک دوست نامه‌ای ناتمام نوشتم و بعد فهمیدم شنیدن مهم‌تر از راه‌حل دادن است."),
    ("friend-moving", "memory", ["دلتنگی", "رفیق", "دوستی", "مهاجرت", "تنهایی"], "داستان پس‌زمینه: دوستی نزدیک از شهر رفت؛ نویا آموخت نبودن آدم‌ها الزاماً به معنی بی‌وفایی نیست."),
    ("dramatic-misread", "drama", ["دراما", "درام", "دعوا", "قهر", "سوء تفاهم"], "گره داستانی: یک سوءتفاهم پیامکی در یک دوستی، چند هفته دلخوری به‌وجود آورد و بعد با صحبت مستقیم حل شد؛ نویا در قضاوت شتاب نمی‌کند."),
    ("vulnerability", "backstory", ["تروما", "آسیب", "ترس قدیمی", "گذشته تلخ"], "بخشی از پیشینه داستانی: ترس از نادیده گرفته شدن پس از قطع ناگهانی یک رابطه دوستانه. این صرفاً عنصر روایت است، نه سابقه بالینی یا تجربه واقعی."),
    ("values", "values", ["ارزش", "باور", "مرز", "صداقت"], "ارزش‌های شخصیت نویا: احترام، کنجکاوی، پاسخگویی در برابر اشتباه و حق انتخاب مستقل مخاطب."),
]


def seed_persona(apps, schema_editor):
    Fact = apps.get_model("botapp", "NoyaPersonaFact")
    for key, category, triggers, value in PERSONA_FACTS:
        Fact.objects.get_or_create(
            key=key,
            defaults={"category": category, "triggers": triggers, "value": value,
                      "fictional": True, "enabled": True},
        )


class Migration(migrations.Migration):
    dependencies = [("botapp", "0033_noyarelationshipstate_conflict_started_at_and_more")]

    operations = [
        migrations.CreateModel(
            name="NoyaAffectProfile",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("scores", models.JSONField(blank=True, default=dict)),
                ("processed_message_keys", models.JSONField(blank=True, default=list)),
                ("last_affective_at", models.DateTimeField(blank=True, null=True)),
                ("adult_confirmed", models.BooleanField(default=False)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("user", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="noya_affect_profile", to="botapp.telegramuser")),
            ],
        ),
        migrations.CreateModel(
            name="NoyaPersonaFact",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("key", models.SlugField(max_length=80, unique=True)),
                ("category", models.CharField(db_index=True, max_length=32)),
                ("triggers", models.JSONField(blank=True, default=list)),
                ("value", models.TextField(max_length=1200)),
                ("fictional", models.BooleanField(default=True)),
                ("enabled", models.BooleanField(default=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.RunPython(seed_persona, migrations.RunPython.noop),
    ]
