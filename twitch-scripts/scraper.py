import asyncio
import json
import re
from json import JSONDecodeError
from typing import AsyncGenerator
from typing import TypedDict

from aiohttp import ClientSession
from bs4 import BeautifulSoup
from bs4 import PageElement
from bs4.element import NavigableString
from bs4.element import Tag
from yarl import URL


class HttpField(TypedDict, total=False):
    key: str
    type: str
    required: bool
    description: str
    default_value: str
    possible_values: list[str]


class EndpointDoc(TypedDict, total=False):
    summary: str
    url: str
    method: str
    title: str
    scopes: list[str]
    request_body: list[HttpField]
    response_body: list[HttpField]
    query_params: list[HttpField]
    example_response: dict


def pull_scopes(tag: Tag) -> list[str]:
    scopes: list[str] = []

    for scope_tag in tag.find_all("strong"):
        scopes.append(scope_tag.get_text(strip=True, separator=" ").casefold())

    return scopes


def pull_possible_values_list(tag: Tag) -> list[str]:
    values_list_tag = tag.find("ul")
    container: list[str] = []

    if values_list_tag is None:
        return []

    for child in values_list_tag.children:
        if not isinstance(child, Tag):
            continue

        value_text = child.get_text(separator=" ", strip=True)

        if "\u2014" in value_text:
            container.append(value_text.split(" \u2014 ")[0])
        elif "-" in value_text:
            container.append(value_text.split(" - ")[0])
        else:
            container.append(value_text)

    return container


def parse_request_body(tag: Tag) -> list[HttpField]:
    # noinspection PyTypeChecker
    container: list[HttpField] = []
    body_tag = tag.find("tbody")

    for row in body_tag.children:
        if isinstance(row, NavigableString):
            continue

        row: Tag

        table_data = [child for child in row.children if isinstance(child, Tag)]
        field: HttpField = {
            "key": table_data[0].get_text(strip=True, separator=" "),
            "type": table_data[1].get_text(strip=True, separator=" ").casefold()
        }

        if len(table_data) == 4:  # We have a "required" column
            bool_text = table_data[2].get_text(strip=True).casefold()
            field["description"] = table_data[3].get_text(strip=True, separator=" ")
            raw_description = table_data[3]

            match bool_text:
                case "yes":
                    field["required"] = True
                case "no":
                    field["required"] = False
        else:
            field["required"] = False
            field["description"] = table_data[2].get_text(strip=True, separator=" ")
            raw_description = table_data[2]

        if "Possible values" in field["description"] and field["type"] == "string".casefold():
            field["possible_values"] = pull_possible_values_list(raw_description)

        if "The default is" in field["description"]:
            try:
                index = field["description"].index("The default is")
            except ValueError:
                pass
            else:
                field["default_value"] = field["description"][index + len("the default is") + 1:].split(" ")[0]

                if field["default_value"].startswith('"'):
                    field["default_value"] = field["default_value"][1:-1]

                field["default_value"] = field["default_value"].rstrip(".,")

        container.append(field)

    return container


# noinspection PyTypeChecker
def parse_response_body(tag: Tag) -> list[HttpField]:
    container: list[HttpField] = []
    body_tag = tag.find("tbody")
    key_format_string: str | None = None

    for row in body_tag.children:
        if isinstance(row, NavigableString):
            continue

        row: Tag

        data: list[Tag] = [child for child in row.children if isinstance(child, Tag)]
        key = data[0].get_text(strip=True, separator=" ")
        data_type = data[1].get_text(strip=True, separator=" ")
        description = data[2].get_text(strip=True, separator=" ")

        field: HttpField = {"key": (key if key_format_string is None else key_format_string.format(key)).casefold(),
                            "type": data_type.casefold(), "description": description, }

        container.append(field)

        if field["key"].casefold().endswith("[]".casefold()):  # We're in an array.
            key_format_string = f"{key}[#]." + "{0}"
        elif field["key"].casefold().startswith("Object".casefold()):
            key_format_string = f"{key}." + "{0}"

    return container


def scrape_doc_left_column(tag: Tag) -> EndpointDoc:
    endpoint_doc: EndpointDoc = {}
    current_header: str | None = None

    for child in tag.children:
        child: Tag

        if child.name == "h2" and child.attrs.get("id", None):
            endpoint_doc["title"] = child.get_text(strip=True, separator=" ")
            current_header = endpoint_doc["title"]
        elif child.name == "p" and current_header == endpoint_doc["title"]:
            if not endpoint_doc.get("summary", None):
                endpoint_doc["summary"] = ""

            endpoint_doc["summary"] = endpoint_doc["summary"] + "\n\n" + child.get_text(strip=True, separator=" ")
            endpoint_doc["summary"] = endpoint_doc["summary"].strip()
        elif child.name == "h3":
            current_header = child.get_text(strip=True, separator=" ")
        elif child.name == "p" and current_header == "Authorization":
            endpoint_doc["scopes"] = pull_scopes(child)
        elif child.name == "p" and current_header == "URL":
            segments = child.get_text(strip=True, separator=" ").split(" ")

            if len(segments) == 2:
                http_method = segments[0]
                url = segments[1]
            else:
                http_method = "GET"
                url = segments[0]

            endpoint_doc["method"] = http_method
            endpoint_doc["url"] = url
        elif child.name == "table" and current_header == "Request Body":
            endpoint_doc["request_body"] = parse_request_body(child)
        elif child.name == "table" and current_header == "Request Query Parameters":
            endpoint_doc["query_params"] = parse_request_body(child)
        elif child.name == "table" and current_header == "Response Body":
            endpoint_doc["response_body"] = parse_response_body(child)

    return endpoint_doc


def fix_json_property(json_string: str, property_name: str) -> str:
    string_copy = json_string
    index: int = 0

    while True:
        try:
            index = string_copy[index:].index(F'"{property_name}"')
        except ValueError:
            return string_copy

        if string_copy[index - 1] not in [",", "{"]:
            string_copy = string_copy[:index] + "," + string_copy[index:]
        else:
            return string_copy


def scrape_doc_section(tag: PageElement) -> EndpointDoc:
    left_doc_column = tag.find_next("section", attrs={"class": "left-docs"})

    left_doc_scrape_result: EndpointDoc | None = None
    if left_doc_column is not None:
        left_doc_scrape_result = scrape_doc_left_column(left_doc_column)

    right_doc_column = tag.find_next("section", attrs={"class": "right-code"})
    right_response_column = right_doc_column.find("div", attrs={"class": "language-json"})

    if right_response_column is not None:
        right_column_text = right_response_column.get_text(strip=True)
        right_column_text = right_column_text.replace("...", "")
        right_column_text = right_column_text.replace(",]", "]")
        right_column_text = right_column_text.replace(",}", "}")
        right_column_text = fix_json_property(right_column_text, "data")
        right_column_text = fix_json_property(right_column_text, "click_action")

        try:
            left_doc_scrape_result["example_response"] = json.loads(right_column_text)
        except JSONDecodeError as e:
            print("ERR", right_column_text[e.colno- 1:])
            print(right_column_text)

    return left_doc_scrape_result


async def scrape_docs(url: URL) -> AsyncGenerator[EndpointDoc, None]:
    async with ClientSession() as session:
        async with session.get(url) as response:
            if not response.ok:
                return

            soup = BeautifulSoup(await response.text(), "lxml")
            doc_section = soup.find("body").find("div", attrs={"class": "main"})

            at_endpoint_definition_table = True
            for child in doc_section.children:
                child: Tag

                if at_endpoint_definition_table or child.name is None or child.name.casefold() != "section".casefold():
                    at_endpoint_definition_table = False

                    continue

                yield scrape_doc_section(child)


async def main():
    endpoints: list[EndpointDoc] = []
    async for result in scrape_docs(URL("https://dev.twitch.tv/docs/api/reference")):
        if not result:
            continue

        endpoints.append(result)

    with open("endpoints.json", "w") as file:
        json.dump(endpoints, file, indent=2)


if __name__ == '__main__':
    asyncio.run(main())
