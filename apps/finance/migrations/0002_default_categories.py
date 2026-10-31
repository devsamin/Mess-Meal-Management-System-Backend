from django.db import migrations

DEFAULTS = [
    # name, code, in meal rate
    ("Bazar", "bazar", True),
    ("Gas", "gas", True),
    ("Electricity", "electricity", False),
    ("Water", "water", False),
    ("Internet", "internet", False),
    ("Cleaning", "cleaning", False),
    ("Other", "other", False),
]


def create_categories(apps, schema_editor):
    ExpenseCategory = apps.get_model("finance", "ExpenseCategory")
    for order, (name, code, in_rate) in enumerate(DEFAULTS):
        ExpenseCategory.objects.get_or_create(
            code=code, defaults={"name": name, "include_in_meal_rate": in_rate, "sort_order": order}
        )


class Migration(migrations.Migration):
    dependencies = [("finance", "0001_initial")]

    operations = [migrations.RunPython(create_categories, migrations.RunPython.noop)]
