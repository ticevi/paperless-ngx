# -*- coding: utf-8 -*-
# Generated by Django 1.9 on 2016-01-23 03:13
from __future__ import unicode_literals

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("documents", "0004_auto_20160114_1844"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="sender",
            options={"ordering": ("name",)},
        ),
    ]
