import logging
import re

from documents.models import Correspondent
from documents.models import DocumentType
from documents.models import MatchingModel
from documents.models import StoragePath
from documents.models import Tag


logger = logging.getLogger("paperless.matching")


def log_reason(matching_model, document, reason):
    class_name = type(matching_model).__name__
    logger.debug(
        f"{class_name} {matching_model.name} matched on document "
        f"{document} because {reason}",
    )


def match_correspondents(document, classifier):
    pred_id = classifier.predict_correspondent(document.content) if classifier else None

    correspondents = Correspondent.objects.all()

    return list(
        filter(lambda o: matches(o, document) or o.pk == pred_id, correspondents),
    )


def match_document_types(document, classifier):
    pred_id = classifier.predict_document_type(document.content) if classifier else None

    document_types = DocumentType.objects.all()

    return list(
        filter(lambda o: matches(o, document) or o.pk == pred_id, document_types),
    )


def match_tags(document, classifier):
    predicted_tag_ids = classifier.predict_tags(document.content) if classifier else []

    tags = Tag.objects.all()

    return list(
        filter(lambda o: matches(o, document) or o.pk in predicted_tag_ids, tags),
    )


def match_storage_paths(document, classifier):
    pred_id = classifier.predict_storage_path(document.content) if classifier else None

    storage_paths = StoragePath.objects.all()

    return list(
        filter(
            lambda o: matches(o, document) or o.pk == pred_id,
            storage_paths,
        ),
    )


def matches(matching_model, document):
    search_kwargs = {}

    document_content = document.content

    # Check that match is not empty
    if not matching_model.match.strip():
        return False

    if matching_model.is_insensitive:
        search_kwargs = {"flags": re.IGNORECASE}

    if matching_model.matching_algorithm == MatchingModel.MATCH_NONE:
        return False

    elif matching_model.matching_algorithm == MatchingModel.MATCH_ALL:
        for word in _split_match(matching_model):
            search_result = re.search(rf"\b{word}\b", document_content, **search_kwargs)
            if not search_result:
                return False
        log_reason(
            matching_model,
            document,
            f"it contains all of these words: {matching_model.match}",
        )
        return True

    elif matching_model.matching_algorithm == MatchingModel.MATCH_ANY:
        for word in _split_match(matching_model):
            if re.search(rf"\b{word}\b", document_content, **search_kwargs):
                log_reason(matching_model, document, f"it contains this word: {word}")
                return True
        return False

    elif matching_model.matching_algorithm == MatchingModel.MATCH_LITERAL:
        result = bool(
            re.search(
                rf"\b{re.escape(matching_model.match)}\b",
                document_content,
                **search_kwargs,
            ),
        )
        if result:
            log_reason(
                matching_model,
                document,
                f'it contains this string: "{matching_model.match}"',
            )
        return result

    elif matching_model.matching_algorithm == MatchingModel.MATCH_REGEX:
        try:
            match = re.search(
                re.compile(matching_model.match, **search_kwargs),
                document_content,
            )
        except re.error:
            logger.error(
                f"Error while processing regular expression {matching_model.match}",
            )
            return False
        if match:
            log_reason(
                matching_model,
                document,
                f"the string {match.group()} matches the regular expression "
                f"{matching_model.match}",
            )
        return bool(match)

    elif matching_model.matching_algorithm == MatchingModel.MATCH_FUZZY:
        from rapidfuzz import fuzz

        match = re.sub(r"[^\w\s]", "", matching_model.match)
        text = re.sub(r"[^\w\s]", "", document_content)
        if matching_model.is_insensitive:
            match = match.lower()
            text = text.lower()
        if fuzz.partial_ratio(match, text, score_cutoff=90):
            # TODO: make this better
            log_reason(
                matching_model,
                document,
                f"parts of the document content somehow match the string "
                f"{matching_model.match}",
            )
            return True
        else:
            return False

    elif matching_model.matching_algorithm == MatchingModel.MATCH_AUTO:
        # this is done elsewhere.
        return False

    else:
        raise NotImplementedError("Unsupported matching algorithm")


def _split_match(matching_model):
    """
    Splits the match to individual keywords, getting rid of unnecessary
    spaces and grouping quoted words together.

    Example:
      '  some random  words "with   quotes  " and   spaces'
        ==>
      ["some", "random", "words", "with+quotes", "and", "spaces"]
    """
    findterms = re.compile(r'"([^"]+)"|(\S+)').findall
    normspace = re.compile(r"\s+").sub
    return [
        # normspace(" ", (t[0] or t[1]).strip()).replace(" ", r"\s+")
        re.escape(normspace(" ", (t[0] or t[1]).strip())).replace(r"\ ", r"\s+")
        for t in findterms(matching_model.match)
    ]
