# Generated by Django 3.0.12 on 2021-03-22 16:00

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("pretixbase", "0179_auto_20210311_1653"),
    ]

    operations = [
        migrations.CreateModel(
            name="ReferencedPayoneObject",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True, primary_key=True, serialize=False
                    ),
                ),
                ("txid", models.CharField(db_index=True, max_length=190, unique=True)),
                (
                    "order",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="pretixbase.Order",
                    ),
                ),
                (
                    "payment",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="pretixbase.OrderPayment",
                    ),
                ),
            ],
        ),
    ]
