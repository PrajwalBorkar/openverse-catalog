import logging
import re
from urllib.parse import parse_qs, urlparse

from airflow.models import Variable
from common.licenses import get_license_info
from common.loader import provider_details as prov
from common.requester import DelayedRequester
from common.storage.image import ImageStore


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s:  %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

LIMIT = 500
DELAY = 1.0
RETRIES = 3
PROVIDER = prov.NYPL_DEFAULT_PROVIDER
BASE_ENDPOINT = "http://api.repo.nypl.org/api/v1/items/search"
METADATA_ENDPOINT = "http://api.repo.nypl.org/api/v1/items/item_details/"
NYPL_API = Variable.get("API_KEY_NYPL", default_var=None)
TOKEN = f"Token token={NYPL_API}"
FILETYPE_PATTERN = r" .(jpeg|gif) "

delay_request = DelayedRequester(delay=DELAY)
image_store = ImageStore(provider=PROVIDER)

DEFAULT_QUERY_PARAMS = {
    "q": "CC_0",
    "field": "use_rtxt_s",
    "page": 1,
    "per_page": LIMIT,
}

HEADERS = {"Authorization": TOKEN}

IMAGE_URL_DIMENSIONS = ["g", "v", "q", "w", "r"]


def main():
    page = 1
    condition = True
    while condition:
        query_param = _get_query_param(page=page)
        request_response = _request_handler(params=query_param)
        results = request_response.get("result")
        if type(results) == list and len(results) > 0:
            _handle_results(results)
            logger.info(f"{image_store.total_items} images till now")
            page = page + 1
        else:
            condition = False
    image_store.commit()
    logger.info(f"total images {image_store.total_items}")


def _get_query_param(
    default_query_params=None,
    page=1,
):
    if default_query_params is None:
        default_query_params = DEFAULT_QUERY_PARAMS
    query_param = default_query_params.copy()
    query_param["page"] = page
    return query_param


def _request_handler(
    endpoint=BASE_ENDPOINT, params=None, headers=None, retries=RETRIES
):
    if headers is None:
        headers = HEADERS.copy()
    results = None
    for retry in range(retries):
        response = delay_request.get(endpoint, params=params, headers=headers)
        if response.status_code == 200:
            try:
                response_json = response.json()
                response_json = response_json.get("nyplAPI")
                results = response_json.get("response")
                break

            except Exception as e:
                logger.warning(f"Request failed due to {e}")
                results = None
        else:
            results = None
    return results


def _handle_results(results):
    for item in results:
        uuid = item.get("uuid")

        item_details = _request_handler(
            endpoint=METADATA_ENDPOINT + uuid,
        )
        if item_details is None:
            continue

        mods = item_details.get("mods")
        title = _get_title(mods.get("titleInfo"))
        creator = _get_creators(mods.get("name"))
        metadata = _get_metadata(mods)

        captures = item_details.get("sibling_captures", {}).get("capture", [])
        if type(captures) is not list:
            captures = [captures]

        _get_capture_details(
            captures=captures, metadata=metadata, creator=creator, title=title
        )


def _get_capture_details(captures=None, metadata=None, creator=None, title=None):
    if captures is None:
        captures = []
    for img in captures:
        image_id = img.get("imageID", {}).get("$")
        if image_id is None:
            continue
        image_url, filetype = _get_image_data(
            img.get("imageLinks", {}).get("imageLink", [])
        )
        foreign_landing_url = img.get("itemLink", {}).get("$")
        license_url = img.get("rightsStatementURI", {}).get("$")
        if image_url is None or foreign_landing_url is None or license_url is None:
            continue

        image_store.add_item(
            foreign_identifier=image_id,
            foreign_landing_url=foreign_landing_url,
            image_url=image_url,
            filetype=filetype,
            license_info=get_license_info(license_url=license_url),
            title=title,
            creator=creator,
            meta_data=metadata,
        )


def _get_title(titleinfo):
    title = None
    if type(titleinfo) == list and len(titleinfo) > 0:
        title = titleinfo[0].get("title", {}).get("$")
    return title


def _get_creators(creatorinfo):
    if type(creatorinfo) == list:
        primary_creator = (
            info.get("namePart", {}).get("$")
            for info in creatorinfo
            if info.get("usage") == "primary"
        )
        creator = next(primary_creator, None)
    else:
        creator = None

    if creator is None:
        logger.debug("No primary creator found")

    return creator


def _get_filetype(description: str):
    """
    Extracts the filetype from a description string like:
    "Cropped .jpeg (1600 pixels on the long side)"
    :param description: the description string
    :return:  jpeg | gif
    """
    if match := re.search(FILETYPE_PATTERN, description):
        return match.group(1)
    return None


def _get_image_data(images):
    """
    Gets a list of dictionaries of the following shape:
    {
      "$": "http://images.nypl.org/index.php?id=56738467&t=q&download=1
    &suffix=29eed1f0-3d50-0134-c4c7-00505686a51c.001",
      "description": "Cropped .jpeg (1600 pixels on the long side)"
    }
    Extracts the largest image based on the `t` query parameter
    and IMAGE_URL_DIMENSIONS.
    """

    image_type = {
        parse_qs(urlparse(img["$"]).query)["t"][0]: {
            "url": img["$"],
            "description": img["description"],
        }
        for img in images
    }
    if image_type == {}:
        return None, None
    preferred_image = (
        (
            image_type[dimension]["url"].replace("&download=1", ""),
            _get_filetype(image_type[dimension]["description"]),
        )
        for dimension in IMAGE_URL_DIMENSIONS
        if dimension in image_type
    )
    image_url, filetype = next(preferred_image, None)
    return image_url, filetype


def _get_metadata(mods):
    metadata = {}

    type_of_resource = mods.get("typeOfResource")
    if isinstance(type_of_resource, list) and (
        type_of_resource[0].get("usage") == "primary"
    ):
        metadata["type_of_resource"] = type_of_resource[0].get("$")

    if isinstance(mods.get("genre"), dict):
        metadata["genre"] = mods.get("genre").get("$")

    origin_info = mods.get("originInfo", {})
    if date_issued := origin_info.get("dateIssued", {}).get("$"):
        metadata["date_issued"] = date_issued
    if publisher := origin_info.get("publisher", {}).get("$"):
        metadata["publisher"] = publisher
    if description := mods.get("physicalDescription", {}).get("note", {}).get("$"):
        metadata["description"] = description

    return metadata


if __name__ == "__main__":
    main()
