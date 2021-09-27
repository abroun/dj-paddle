# Generated by Django 3.2.6 on 2021-09-27 16:36

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('djpaddle', '0004_auto_20210119_0436'),
    ]

    operations = [
        migrations.CreateModel(
            name='ReplayedEvent',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('time', models.DateTimeField()),
                ('payload', models.JSONField()),
            ],
        ),
        migrations.CreateModel(
            name='WebhookEvent',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('time', models.DateTimeField()),
                ('payload', models.JSONField()),
            ],
        ),
    ]
