import datetime
import math
import re

from celery import states

try:
    import zoneinfo
except ImportError:
    from backports import zoneinfo
import magic
from django.conf import settings
from django.utils.text import slugify
from django.utils.translation import gettext as _
from rest_framework import serializers
from rest_framework.fields import SerializerMethodField

from . import bulk_edit
from .models import Correspondent
from .models import Document
from .models import DocumentType
from .models import MatchingModel
from .models import SavedView
from .models import SavedViewFilterRule
from .models import StoragePath
from .models import Tag
from .models import UiSettings
from .models import PaperlessTask
from .parsers import is_mime_type_supported

from guardian.shortcuts import get_users_with_perms

from django.contrib.auth.models import User
from django.contrib.auth.models import Group

from documents.permissions import get_groups_with_only_permission
from documents.permissions import set_permissions_for_object


# https://www.django-rest-framework.org/api-guide/serializers/#example
class DynamicFieldsModelSerializer(serializers.ModelSerializer):
    """
    A ModelSerializer that takes an additional `fields` argument that
    controls which fields should be displayed.
    """

    def __init__(self, *args, **kwargs):
        # Don't pass the 'fields' arg up to the superclass
        fields = kwargs.pop("fields", None)

        # Instantiate the superclass normally
        super().__init__(*args, **kwargs)

        if fields is not None:
            # Drop any fields that are not specified in the `fields` argument.
            allowed = set(fields)
            existing = set(self.fields)
            for field_name in existing - allowed:
                self.fields.pop(field_name)


class MatchingModelSerializer(serializers.ModelSerializer):

    document_count = serializers.IntegerField(read_only=True)

    def get_slug(self, obj):
        return slugify(obj.name)

    slug = SerializerMethodField()

    def validate(self, data):
        # see https://github.com/encode/django-rest-framework/issues/7173
        name = data["name"] if "name" in data else self.instance.name
        owner = (
            data["owner"]
            if "owner" in data
            else self.user
            if hasattr(self, "user")
            else None
        )
        pk = self.instance.pk if hasattr(self.instance, "pk") else None
        if ("name" in data or "owner" in data) and self.Meta.model.objects.filter(
            name=name,
            owner=owner,
        ).exclude(pk=pk).exists():
            raise serializers.ValidationError(
                {"error": "Object violates owner / name unique constraint"},
            )
        return data

    def validate_match(self, match):
        if (
            "matching_algorithm" in self.initial_data
            and self.initial_data["matching_algorithm"] == MatchingModel.MATCH_REGEX
        ):
            try:
                re.compile(match)
            except re.error as e:
                raise serializers.ValidationError(
                    _("Invalid regular expression: %(error)s") % {"error": str(e.msg)},
                )
        return match


class SetPermissionsMixin:
    def _validate_user_ids(self, user_ids):
        users = User.objects.none()
        if user_ids is not None:
            users = User.objects.filter(id__in=user_ids)
            if not users.count() == len(user_ids):
                raise serializers.ValidationError(
                    "Some users in don't exist or were specified twice.",
                )
        return users

    def _validate_group_ids(self, group_ids):
        groups = Group.objects.none()
        if group_ids is not None:
            groups = Group.objects.filter(id__in=group_ids)
            if not groups.count() == len(group_ids):
                raise serializers.ValidationError(
                    "Some groups in don't exist or were specified twice.",
                )
        return groups

    def validate_set_permissions(self, set_permissions=None):
        permissions_dict = {
            "view": {
                "users": User.objects.none(),
                "groups": Group.objects.none(),
            },
            "change": {
                "users": User.objects.none(),
                "groups": Group.objects.none(),
            },
        }
        if set_permissions is not None:
            for action in permissions_dict:
                if action in set_permissions:
                    users = set_permissions[action]["users"]
                    permissions_dict[action]["users"] = self._validate_user_ids(users)
                    groups = set_permissions[action]["groups"]
                    permissions_dict[action]["groups"] = self._validate_group_ids(
                        groups,
                    )
        return permissions_dict

    def _set_permissions(self, permissions, object):
        set_permissions_for_object(permissions, object)


class OwnedObjectSerializer(serializers.ModelSerializer, SetPermissionsMixin):
    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)

    def get_permissions(self, obj):
        view_codename = f"view_{obj.__class__.__name__.lower()}"
        change_codename = f"change_{obj.__class__.__name__.lower()}"
        return {
            "view": {
                "users": get_users_with_perms(
                    obj,
                    only_with_perms_in=[view_codename],
                ).values_list("id", flat=True),
                "groups": get_groups_with_only_permission(
                    obj,
                    codename=view_codename,
                ).values_list("id", flat=True),
            },
            "change": {
                "users": get_users_with_perms(
                    obj,
                    only_with_perms_in=[change_codename],
                ).values_list("id", flat=True),
                "groups": get_groups_with_only_permission(
                    obj,
                    codename=change_codename,
                ).values_list("id", flat=True),
            },
        }

    permissions = SerializerMethodField(read_only=True)

    set_permissions = serializers.DictField(
        label="Set permissions",
        allow_empty=True,
        required=False,
        write_only=True,
    )
    # other methods in mixin

    def create(self, validated_data):
        if self.user and (
            "owner" not in validated_data or validated_data["owner"] is None
        ):
            validated_data["owner"] = self.user
        permissions = None
        if "set_permissions" in validated_data:
            permissions = validated_data.pop("set_permissions")
        instance = super().create(validated_data)
        if permissions is not None:
            self._set_permissions(permissions, instance)
        return instance

    def update(self, instance, validated_data):
        if "set_permissions" in validated_data:
            self._set_permissions(validated_data["set_permissions"], instance)
        if "owner" in validated_data and "name" in self.Meta.fields:
            name = validated_data["name"] if "name" in validated_data else instance.name
            not_unique = (
                self.Meta.model.objects.exclude(pk=instance.pk)
                .filter(owner=validated_data["owner"], name=name)
                .exists()
            )
            if not_unique:
                raise serializers.ValidationError(
                    {"error": "Object violates owner / name unique constraint"},
                )
        return super().update(instance, validated_data)


class CorrespondentSerializer(MatchingModelSerializer, OwnedObjectSerializer):

    last_correspondence = serializers.DateTimeField(read_only=True)

    class Meta:
        model = Correspondent
        fields = (
            "id",
            "slug",
            "name",
            "match",
            "matching_algorithm",
            "is_insensitive",
            "document_count",
            "last_correspondence",
            "owner",
            "permissions",
            "set_permissions",
        )


class DocumentTypeSerializer(MatchingModelSerializer, OwnedObjectSerializer):
    class Meta:
        model = DocumentType
        fields = (
            "id",
            "slug",
            "name",
            "match",
            "matching_algorithm",
            "is_insensitive",
            "document_count",
            "owner",
            "permissions",
            "set_permissions",
        )


class ColorField(serializers.Field):

    COLOURS = (
        (1, "#a6cee3"),
        (2, "#1f78b4"),
        (3, "#b2df8a"),
        (4, "#33a02c"),
        (5, "#fb9a99"),
        (6, "#e31a1c"),
        (7, "#fdbf6f"),
        (8, "#ff7f00"),
        (9, "#cab2d6"),
        (10, "#6a3d9a"),
        (11, "#b15928"),
        (12, "#000000"),
        (13, "#cccccc"),
    )

    def to_internal_value(self, data):
        for id, color in self.COLOURS:
            if id == data:
                return color
        raise serializers.ValidationError

    def to_representation(self, value):
        for id, color in self.COLOURS:
            if color == value:
                return id
        return 1


class TagSerializerVersion1(MatchingModelSerializer, OwnedObjectSerializer):

    colour = ColorField(source="color", default="#a6cee3")

    class Meta:
        model = Tag
        fields = (
            "id",
            "slug",
            "name",
            "colour",
            "match",
            "matching_algorithm",
            "is_insensitive",
            "is_inbox_tag",
            "document_count",
            "owner",
            "permissions",
            "set_permissions",
        )


class TagSerializer(MatchingModelSerializer, OwnedObjectSerializer):
    def get_text_color(self, obj):
        try:
            h = obj.color.lstrip("#")
            rgb = tuple(int(h[i : i + 2], 16) / 256 for i in (0, 2, 4))
            luminance = math.sqrt(
                0.299 * math.pow(rgb[0], 2)
                + 0.587 * math.pow(rgb[1], 2)
                + 0.114 * math.pow(rgb[2], 2),
            )
            return "#ffffff" if luminance < 0.53 else "#000000"
        except ValueError:
            return "#000000"

    text_color = serializers.SerializerMethodField()

    class Meta:
        model = Tag
        fields = (
            "id",
            "slug",
            "name",
            "color",
            "text_color",
            "match",
            "matching_algorithm",
            "is_insensitive",
            "is_inbox_tag",
            "document_count",
            "owner",
            "permissions",
            "set_permissions",
        )

    def validate_color(self, color):
        regex = r"#[0-9a-fA-F]{6}"
        if not re.match(regex, color):
            raise serializers.ValidationError(_("Invalid color."))
        return color


class CorrespondentField(serializers.PrimaryKeyRelatedField):
    def get_queryset(self):
        return Correspondent.objects.all()


class TagsField(serializers.PrimaryKeyRelatedField):
    def get_queryset(self):
        return Tag.objects.all()


class DocumentTypeField(serializers.PrimaryKeyRelatedField):
    def get_queryset(self):
        return DocumentType.objects.all()


class StoragePathField(serializers.PrimaryKeyRelatedField):
    def get_queryset(self):
        return StoragePath.objects.all()


class DocumentSerializer(OwnedObjectSerializer, DynamicFieldsModelSerializer):

    correspondent = CorrespondentField(allow_null=True)
    tags = TagsField(many=True)
    document_type = DocumentTypeField(allow_null=True)
    storage_path = StoragePathField(allow_null=True)

    original_file_name = SerializerMethodField()
    archived_file_name = SerializerMethodField()
    created_date = serializers.DateField(required=False)

    owner = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.all(),
        required=False,
        allow_null=True,
    )

    def get_original_file_name(self, obj):
        return obj.get_public_filename()

    def get_archived_file_name(self, obj):
        if obj.has_archive_version:
            return obj.get_public_filename(archive=True)
        else:
            return None

    def to_representation(self, instance):
        doc = super().to_representation(instance)
        if self.truncate_content:
            doc["content"] = doc.get("content")[0:550]
        return doc

    def update(self, instance, validated_data):
        if "created_date" in validated_data and "created" not in validated_data:
            new_datetime = datetime.datetime.combine(
                validated_data.get("created_date"),
                datetime.time(0, 0, 0, 0, zoneinfo.ZoneInfo(settings.TIME_ZONE)),
            )
            instance.created = new_datetime
            instance.save()
        if "created_date" in validated_data:
            validated_data.pop("created_date")
        super().update(instance, validated_data)
        return instance

    def __init__(self, *args, **kwargs):
        self.truncate_content = kwargs.pop("truncate_content", False)

        super().__init__(*args, **kwargs)

    class Meta:
        model = Document
        depth = 1
        fields = (
            "id",
            "correspondent",
            "document_type",
            "storage_path",
            "title",
            "content",
            "tags",
            "created",
            "created_date",
            "modified",
            "added",
            "archive_serial_number",
            "original_file_name",
            "archived_file_name",
            "owner",
            "permissions",
            "set_permissions",
            "notes",
        )


class SavedViewFilterRuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = SavedViewFilterRule
        fields = ["rule_type", "value"]


class SavedViewSerializer(OwnedObjectSerializer):

    filter_rules = SavedViewFilterRuleSerializer(many=True)

    class Meta:
        model = SavedView
        depth = 1
        fields = [
            "id",
            "name",
            "show_on_dashboard",
            "show_in_sidebar",
            "sort_field",
            "sort_reverse",
            "filter_rules",
            "owner",
            "permissions",
            "set_permissions",
        ]

    def update(self, instance, validated_data):
        if "filter_rules" in validated_data:
            rules_data = validated_data.pop("filter_rules")
        else:
            rules_data = None
        if "user" in validated_data:
            # backwards compatibility
            validated_data["owner"] = validated_data.pop("user")
        super().update(instance, validated_data)
        if rules_data is not None:
            SavedViewFilterRule.objects.filter(saved_view=instance).delete()
            for rule_data in rules_data:
                SavedViewFilterRule.objects.create(saved_view=instance, **rule_data)
        return instance

    def create(self, validated_data):
        rules_data = validated_data.pop("filter_rules")
        if "user" in validated_data:
            # backwards compatibility
            validated_data["owner"] = validated_data.pop("user")
        saved_view = SavedView.objects.create(**validated_data)
        for rule_data in rules_data:
            SavedViewFilterRule.objects.create(saved_view=saved_view, **rule_data)
        return saved_view


class DocumentListSerializer(serializers.Serializer):

    documents = serializers.ListField(
        required=True,
        label="Documents",
        write_only=True,
        child=serializers.IntegerField(),
    )

    def _validate_document_id_list(self, documents, name="documents"):
        if not type(documents) == list:
            raise serializers.ValidationError(f"{name} must be a list")
        if not all(type(i) == int for i in documents):
            raise serializers.ValidationError(f"{name} must be a list of integers")
        count = Document.objects.filter(id__in=documents).count()
        if not count == len(documents):
            raise serializers.ValidationError(
                f"Some documents in {name} don't exist or were specified twice.",
            )

    def validate_documents(self, documents):
        self._validate_document_id_list(documents)
        return documents


class BulkEditSerializer(DocumentListSerializer, SetPermissionsMixin):

    method = serializers.ChoiceField(
        choices=[
            "set_correspondent",
            "set_document_type",
            "set_storage_path",
            "add_tag",
            "remove_tag",
            "modify_tags",
            "delete",
            "redo_ocr",
            "set_permissions",
        ],
        label="Method",
        write_only=True,
    )

    parameters = serializers.DictField(allow_empty=True)

    def _validate_tag_id_list(self, tags, name="tags"):
        if not type(tags) == list:
            raise serializers.ValidationError(f"{name} must be a list")
        if not all(type(i) == int for i in tags):
            raise serializers.ValidationError(f"{name} must be a list of integers")
        count = Tag.objects.filter(id__in=tags).count()
        if not count == len(tags):
            raise serializers.ValidationError(
                f"Some tags in {name} don't exist or were specified twice.",
            )

    def validate_method(self, method):
        if method == "set_correspondent":
            return bulk_edit.set_correspondent
        elif method == "set_document_type":
            return bulk_edit.set_document_type
        elif method == "set_storage_path":
            return bulk_edit.set_storage_path
        elif method == "add_tag":
            return bulk_edit.add_tag
        elif method == "remove_tag":
            return bulk_edit.remove_tag
        elif method == "modify_tags":
            return bulk_edit.modify_tags
        elif method == "delete":
            return bulk_edit.delete
        elif method == "redo_ocr":
            return bulk_edit.redo_ocr
        elif method == "set_permissions":
            return bulk_edit.set_permissions
        else:
            raise serializers.ValidationError("Unsupported method.")

    def _validate_parameters_tags(self, parameters):
        if "tag" in parameters:
            tag_id = parameters["tag"]
            try:
                Tag.objects.get(id=tag_id)
            except Tag.DoesNotExist:
                raise serializers.ValidationError("Tag does not exist")
        else:
            raise serializers.ValidationError("tag not specified")

    def _validate_parameters_document_type(self, parameters):
        if "document_type" in parameters:
            document_type_id = parameters["document_type"]
            if document_type_id is None:
                # None is ok
                return
            try:
                DocumentType.objects.get(id=document_type_id)
            except DocumentType.DoesNotExist:
                raise serializers.ValidationError("Document type does not exist")
        else:
            raise serializers.ValidationError("document_type not specified")

    def _validate_parameters_correspondent(self, parameters):
        if "correspondent" in parameters:
            correspondent_id = parameters["correspondent"]
            if correspondent_id is None:
                return
            try:
                Correspondent.objects.get(id=correspondent_id)
            except Correspondent.DoesNotExist:
                raise serializers.ValidationError("Correspondent does not exist")
        else:
            raise serializers.ValidationError("correspondent not specified")

    def _validate_storage_path(self, parameters):
        if "storage_path" in parameters:
            storage_path_id = parameters["storage_path"]
            if storage_path_id is None:
                return
            try:
                StoragePath.objects.get(id=storage_path_id)
            except StoragePath.DoesNotExist:
                raise serializers.ValidationError(
                    "Storage path does not exist",
                )
        else:
            raise serializers.ValidationError("storage path not specified")

    def _validate_parameters_modify_tags(self, parameters):
        if "add_tags" in parameters:
            self._validate_tag_id_list(parameters["add_tags"], "add_tags")
        else:
            raise serializers.ValidationError("add_tags not specified")

        if "remove_tags" in parameters:
            self._validate_tag_id_list(parameters["remove_tags"], "remove_tags")
        else:
            raise serializers.ValidationError("remove_tags not specified")

    def _validate_owner(self, owner):
        ownerUser = User.objects.get(pk=owner)
        if ownerUser is None:
            raise serializers.ValidationError("Specified owner cannot be found")
        return ownerUser

    def _validate_parameters_set_permissions(self, parameters):
        parameters["set_permissions"] = self.validate_set_permissions(
            parameters["set_permissions"],
        )
        if "owner" in parameters and parameters["owner"] is not None:
            self._validate_owner(parameters["owner"])

    def validate(self, attrs):

        method = attrs["method"]
        parameters = attrs["parameters"]

        if method == bulk_edit.set_correspondent:
            self._validate_parameters_correspondent(parameters)
        elif method == bulk_edit.set_document_type:
            self._validate_parameters_document_type(parameters)
        elif method == bulk_edit.add_tag or method == bulk_edit.remove_tag:
            self._validate_parameters_tags(parameters)
        elif method == bulk_edit.modify_tags:
            self._validate_parameters_modify_tags(parameters)
        elif method == bulk_edit.set_storage_path:
            self._validate_storage_path(parameters)
        elif method == bulk_edit.set_permissions:
            self._validate_parameters_set_permissions(parameters)

        return attrs


class PostDocumentSerializer(serializers.Serializer):

    created = serializers.DateTimeField(
        label="Created",
        allow_null=True,
        write_only=True,
        required=False,
    )

    document = serializers.FileField(
        label="Document",
        write_only=True,
    )

    title = serializers.CharField(
        label="Title",
        write_only=True,
        required=False,
    )

    correspondent = serializers.PrimaryKeyRelatedField(
        queryset=Correspondent.objects.all(),
        label="Correspondent",
        allow_null=True,
        write_only=True,
        required=False,
    )

    document_type = serializers.PrimaryKeyRelatedField(
        queryset=DocumentType.objects.all(),
        label="Document type",
        allow_null=True,
        write_only=True,
        required=False,
    )

    tags = serializers.PrimaryKeyRelatedField(
        many=True,
        queryset=Tag.objects.all(),
        label="Tags",
        write_only=True,
        required=False,
    )

    archive_serial_number = serializers.IntegerField(
        label="ASN",
        write_only=True,
        required=False,
        min_value=Document.ARCHIVE_SERIAL_NUMBER_MIN,
        max_value=Document.ARCHIVE_SERIAL_NUMBER_MAX,
    )

    def validate_document(self, document):
        document_data = document.file.read()
        mime_type = magic.from_buffer(document_data, mime=True)

        if not is_mime_type_supported(mime_type):
            raise serializers.ValidationError(
                _("File type %(type)s not supported") % {"type": mime_type},
            )

        return document.name, document_data

    def validate_correspondent(self, correspondent):
        if correspondent:
            return correspondent.id
        else:
            return None

    def validate_document_type(self, document_type):
        if document_type:
            return document_type.id
        else:
            return None

    def validate_tags(self, tags):
        if tags:
            return [tag.id for tag in tags]
        else:
            return None


class BulkDownloadSerializer(DocumentListSerializer):

    content = serializers.ChoiceField(
        choices=["archive", "originals", "both"],
        default="archive",
    )

    compression = serializers.ChoiceField(
        choices=["none", "deflated", "bzip2", "lzma"],
        default="none",
    )

    follow_formatting = serializers.BooleanField(
        default=False,
    )

    def validate_compression(self, compression):
        import zipfile

        return {
            "none": zipfile.ZIP_STORED,
            "deflated": zipfile.ZIP_DEFLATED,
            "bzip2": zipfile.ZIP_BZIP2,
            "lzma": zipfile.ZIP_LZMA,
        }[compression]


class StoragePathSerializer(MatchingModelSerializer, OwnedObjectSerializer):
    class Meta:
        model = StoragePath
        fields = (
            "id",
            "slug",
            "name",
            "path",
            "match",
            "matching_algorithm",
            "is_insensitive",
            "document_count",
            "owner",
            "permissions",
            "set_permissions",
        )

    def validate_path(self, path):
        try:
            path.format(
                title="title",
                correspondent="correspondent",
                document_type="document_type",
                created="created",
                created_year="created_year",
                created_year_short="created_year_short",
                created_month="created_month",
                created_month_name="created_month_name",
                created_month_name_short="created_month_name_short",
                created_day="created_day",
                added="added",
                added_year="added_year",
                added_year_short="added_year_short",
                added_month="added_month",
                added_month_name="added_month_name",
                added_month_name_short="added_month_name_short",
                added_day="added_day",
                asn="asn",
                tags="tags",
                tag_list="tag_list",
                owner_username="someone",
                original_name="testfile",
            )

        except KeyError as err:
            raise serializers.ValidationError(_("Invalid variable detected.")) from err

        return path

    def update(self, instance, validated_data):
        """
        When a storage path is updated, see if documents
        using it require a rename/move
        """
        doc_ids = [doc.id for doc in instance.documents.all()]
        if len(doc_ids):
            bulk_edit.bulk_update_documents.delay(doc_ids)

        return super().update(instance, validated_data)


class UiSettingsViewSerializer(serializers.ModelSerializer):
    class Meta:
        model = UiSettings
        depth = 1
        fields = [
            "id",
            "settings",
        ]

    def validate_settings(self, settings):
        # we never save update checking backend setting
        if "update_checking" in settings:
            try:
                settings["update_checking"].pop("backend_setting")
            except KeyError:
                pass
        return settings

    def create(self, validated_data):
        ui_settings = UiSettings.objects.update_or_create(
            user=validated_data.get("user"),
            defaults={"settings": validated_data.get("settings", None)},
        )
        return ui_settings


class TasksViewSerializer(serializers.ModelSerializer):
    class Meta:
        model = PaperlessTask
        depth = 1
        fields = (
            "id",
            "task_id",
            "task_file_name",
            "date_created",
            "date_done",
            "type",
            "status",
            "result",
            "acknowledged",
            "related_document",
        )

    type = serializers.SerializerMethodField()

    def get_type(self, obj):
        # just file tasks, for now
        return "file"

    related_document = serializers.SerializerMethodField()
    related_doc_re = re.compile(r"New document id (\d+) created")

    def get_related_document(self, obj):
        result = None
        if obj.status is not None and obj.status == states.SUCCESS:
            try:
                result = self.related_doc_re.search(obj.result).group(1)
            except Exception:
                pass

        return result


class AcknowledgeTasksViewSerializer(serializers.Serializer):

    tasks = serializers.ListField(
        required=True,
        label="Tasks",
        write_only=True,
        child=serializers.IntegerField(),
    )

    def _validate_task_id_list(self, tasks, name="tasks"):
        pass
        if not type(tasks) == list:
            raise serializers.ValidationError(f"{name} must be a list")
        if not all(type(i) == int for i in tasks):
            raise serializers.ValidationError(f"{name} must be a list of integers")
        count = PaperlessTask.objects.filter(id__in=tasks).count()
        if not count == len(tasks):
            raise serializers.ValidationError(
                f"Some tasks in {name} don't exist or were specified twice.",
            )

    def validate_tasks(self, tasks):
        self._validate_task_id_list(tasks)
        return tasks
