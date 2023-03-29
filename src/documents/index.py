import logging
import math
import os
from contextlib import contextmanager

from dateutil.parser import isoparse
from django.conf import settings
from django.utils import timezone
from documents.models import Document
from documents.models import Note
from guardian.shortcuts import get_users_with_perms
from whoosh import classify
from whoosh import highlight
from whoosh import query
from whoosh.fields import BOOLEAN
from whoosh.fields import DATETIME
from whoosh.fields import KEYWORD
from whoosh.fields import NUMERIC
from whoosh.fields import Schema
from whoosh.fields import TEXT
from whoosh.highlight import HtmlFormatter
from whoosh.index import create_in
from whoosh.index import exists_in
from whoosh.index import open_dir
from whoosh.qparser import MultifieldParser
from whoosh.qparser.dateparse import DateParserPlugin
from whoosh.searching import ResultsPage
from whoosh.searching import Searcher
from whoosh.writing import AsyncWriter

logger = logging.getLogger("paperless.index")


def get_schema():
    return Schema(
        id=NUMERIC(stored=True, unique=True),
        title=TEXT(sortable=True),
        content=TEXT(),
        asn=NUMERIC(sortable=True, signed=False),
        correspondent=TEXT(sortable=True),
        correspondent_id=NUMERIC(),
        has_correspondent=BOOLEAN(),
        tag=KEYWORD(commas=True, scorable=True, lowercase=True),
        tag_id=KEYWORD(commas=True, scorable=True),
        has_tag=BOOLEAN(),
        type=TEXT(sortable=True),
        type_id=NUMERIC(),
        has_type=BOOLEAN(),
        created=DATETIME(sortable=True),
        modified=DATETIME(sortable=True),
        added=DATETIME(sortable=True),
        path=TEXT(sortable=True),
        path_id=NUMERIC(),
        has_path=BOOLEAN(),
        notes=TEXT(),
        owner=TEXT(),
        owner_id=NUMERIC(),
        has_owner=BOOLEAN(),
        viewer_id=KEYWORD(commas=True),
    )


def open_index(recreate=False):
    try:
        if exists_in(settings.INDEX_DIR) and not recreate:
            return open_dir(settings.INDEX_DIR, schema=get_schema())
    except Exception:
        logger.exception("Error while opening the index, recreating.")

    if not os.path.isdir(settings.INDEX_DIR):
        os.makedirs(settings.INDEX_DIR, exist_ok=True)
    return create_in(settings.INDEX_DIR, get_schema())


@contextmanager
def open_index_writer(optimize=False):
    writer = AsyncWriter(open_index())

    try:
        yield writer
    except Exception as e:
        logger.exception(str(e))
        writer.cancel()
    finally:
        writer.commit(optimize=optimize)


@contextmanager
def open_index_searcher():
    searcher = open_index().searcher()

    try:
        yield searcher
    finally:
        searcher.close()


def update_document(writer: AsyncWriter, doc: Document):
    tags = ",".join([t.name for t in doc.tags.all()])
    tags_ids = ",".join([str(t.id) for t in doc.tags.all()])
    notes = ",".join([str(c.note) for c in Note.objects.filter(document=doc)])
    asn = doc.archive_serial_number
    if asn is not None and (
        asn < Document.ARCHIVE_SERIAL_NUMBER_MIN
        or asn > Document.ARCHIVE_SERIAL_NUMBER_MAX
    ):
        logger.error(
            f"Not indexing Archive Serial Number {asn} of document {doc.pk}. "
            f"ASN is out of range "
            f"[{Document.ARCHIVE_SERIAL_NUMBER_MIN:,}, "
            f"{Document.ARCHIVE_SERIAL_NUMBER_MAX:,}.",
        )
        asn = 0
    users_with_perms = get_users_with_perms(
        doc,
        only_with_perms_in=["view_document"],
    )
    viewer_ids = ",".join([str(u.id) for u in users_with_perms])
    writer.update_document(
        id=doc.pk,
        title=doc.title,
        content=doc.content,
        correspondent=doc.correspondent.name if doc.correspondent else None,
        correspondent_id=doc.correspondent.id if doc.correspondent else None,
        has_correspondent=doc.correspondent is not None,
        tag=tags if tags else None,
        tag_id=tags_ids if tags_ids else None,
        has_tag=len(tags) > 0,
        type=doc.document_type.name if doc.document_type else None,
        type_id=doc.document_type.id if doc.document_type else None,
        has_type=doc.document_type is not None,
        created=doc.created,
        added=doc.added,
        asn=asn,
        modified=doc.modified,
        path=doc.storage_path.name if doc.storage_path else None,
        path_id=doc.storage_path.id if doc.storage_path else None,
        has_path=doc.storage_path is not None,
        notes=notes,
        owner=doc.owner.username if doc.owner else None,
        owner_id=doc.owner.id if doc.owner else None,
        has_owner=doc.owner is not None,
        viewer_id=viewer_ids if viewer_ids else None,
    )


def remove_document(writer, doc):
    remove_document_by_id(writer, doc.pk)


def remove_document_by_id(writer, doc_id):
    writer.delete_by_term("id", doc_id)


def add_or_update_document(document):
    with open_index_writer() as writer:
        update_document(writer, document)


def remove_document_from_index(document):
    with open_index_writer() as writer:
        remove_document(writer, document)


class DelayedQuery:
    def _get_query(self):
        raise NotImplementedError

    def _get_query_filter(self):
        criterias = []
        for k, v in self.query_params.items():
            if k == "correspondent__id":
                criterias.append(query.Term("correspondent_id", v))
            elif k == "tags__id__all":
                for tag_id in v.split(","):
                    criterias.append(query.Term("tag_id", tag_id))
            elif k == "tags__id__none":
                for tag_id in v.split(","):
                    criterias.append(query.Not(query.Term("tag_id", tag_id)))
            elif k == "document_type__id":
                criterias.append(query.Term("type_id", v))
            elif k == "correspondent__isnull":
                criterias.append(query.Term("has_correspondent", v == "false"))
            elif k == "is_tagged":
                criterias.append(query.Term("has_tag", v == "true"))
            elif k == "document_type__isnull":
                criterias.append(query.Term("has_type", v == "false"))
            elif k == "created__date__lt":
                criterias.append(
                    query.DateRange("created", start=None, end=isoparse(v)),
                )
            elif k == "created__date__gt":
                criterias.append(
                    query.DateRange("created", start=isoparse(v), end=None),
                )
            elif k == "added__date__gt":
                criterias.append(query.DateRange("added", start=isoparse(v), end=None))
            elif k == "added__date__lt":
                criterias.append(query.DateRange("added", start=None, end=isoparse(v)))
            elif k == "storage_path__id":
                criterias.append(query.Term("path_id", v))
            elif k == "storage_path__isnull":
                criterias.append(query.Term("has_path", v == "false"))

        user_criterias = [query.Term("has_owner", False)]
        if "user" in self.query_params:
            user_criterias.append(query.Term("owner_id", self.query_params["user"]))
            user_criterias.append(
                query.Term("viewer_id", str(self.query_params["user"])),
            )
        if len(criterias) > 0:
            criterias.append(query.Or(user_criterias))
            return query.And(criterias)
        else:
            return query.Or(user_criterias)

    def _get_query_sortedby(self):
        if "ordering" not in self.query_params:
            return None, False

        field: str = self.query_params["ordering"]

        sort_fields_map = {
            "created": "created",
            "modified": "modified",
            "added": "added",
            "title": "title",
            "correspondent__name": "correspondent",
            "document_type__name": "type",
            "archive_serial_number": "asn",
        }

        if field.startswith("-"):
            field = field[1:]
            reverse = True
        else:
            reverse = False

        if field not in sort_fields_map:
            return None, False
        else:
            return sort_fields_map[field], reverse

    def __init__(self, searcher: Searcher, query_params, page_size):
        self.searcher = searcher
        self.query_params = query_params
        self.page_size = page_size
        self.saved_results = dict()
        self.first_score = None

    def __len__(self):
        page = self[0:1]
        return len(page)

    def __getitem__(self, item):
        if item.start in self.saved_results:
            return self.saved_results[item.start]

        q, mask = self._get_query()
        sortedby, reverse = self._get_query_sortedby()

        page: ResultsPage = self.searcher.search_page(
            q,
            mask=mask,
            filter=self._get_query_filter(),
            pagenum=math.floor(item.start / self.page_size) + 1,
            pagelen=self.page_size,
            sortedby=sortedby,
            reverse=reverse,
        )
        page.results.fragmenter = highlight.ContextFragmenter(surround=50)
        page.results.formatter = HtmlFormatter(tagname="span", between=" ... ")

        if not self.first_score and len(page.results) > 0 and sortedby is None:
            self.first_score = page.results[0].score

        page.results.top_n = list(
            map(
                lambda hit: (
                    (hit[0] / self.first_score) if self.first_score else None,
                    hit[1],
                ),
                page.results.top_n,
            ),
        )

        self.saved_results[item.start] = page

        return page


class DelayedFullTextQuery(DelayedQuery):
    def _get_query(self):
        q_str = self.query_params["query"]
        qp = MultifieldParser(
            ["content", "title", "correspondent", "tag", "type", "notes"],
            self.searcher.ixreader.schema,
        )
        qp.add_plugin(DateParserPlugin(basedate=timezone.now()))
        q = qp.parse(q_str)

        corrected = self.searcher.correct_query(q, q_str)
        if corrected.query != q:
            corrected.query = corrected.string

        return q, None


class DelayedMoreLikeThisQuery(DelayedQuery):
    def _get_query(self):
        more_like_doc_id = int(self.query_params["more_like_id"])
        content = Document.objects.get(id=more_like_doc_id).content

        docnum = self.searcher.document_number(id=more_like_doc_id)
        kts = self.searcher.key_terms_from_text(
            "content",
            content,
            numterms=20,
            model=classify.Bo1Model,
            normalize=False,
        )
        q = query.Or(
            [query.Term("content", word, boost=weight) for word, weight in kts],
        )
        mask = {docnum}

        return q, mask


def autocomplete(ix, term, limit=10):
    with ix.reader() as reader:
        terms = []
        for (score, t) in reader.most_distinctive_terms(
            "content",
            number=limit,
            prefix=term.lower(),
        ):
            terms.append(t)
        return terms
