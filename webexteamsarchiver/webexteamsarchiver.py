"""Webex Teams Room Archiver.

Copyright (c) 2018-2019 Cisco and/or its affiliates.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:
The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.
THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""
import concurrent.futures
import os
import re
import requests
import shutil
import tarfile
import logging
import json
from collections import namedtuple
from webexteamssdk import WebexTeamsAPI
from webexteamssdk.exceptions import MalformedResponse, ApiError
from .jinja_env import env as jinja_env
from .jinja_env import sanitize_name

__all__ = ['WebexTeamsArchiver', 'File', 'UserNotFound']

File = namedtuple(
    "File", "content_disposition content_length content_type filename deleted")

UserNotFound = namedtuple(
    "UserNotFound", "id emails displayName avatar"
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class WebexTeamsArchiver:
    """
    Initializes object that can be used to archive a Webex Teams room.

    Args:
        access_token: User's personal Webex Teams API bearer token.

    Raises:
        webexteamssdkException: An error occurred calling the Webex Teams API.
    """
    def __init__(self, access_token: str, single_request_timeout: int = 60) -> None:
        self.access_token = access_token
        self.sdk = WebexTeamsAPI(self.access_token, single_request_timeout=single_request_timeout)

    def file_details(self, url: str) -> File:
        """
        Retrieves the file details using the Webex Teams attachments endpoint.

        Args:
            url: The URL of the file found in the files list in the message.
            single_request_timeout: Webex API call single request timeout.

        Raises:
            requests.exceptions.RequestException: Error retrieving file details.
            AttributeError: Response missing headers.
            KeyError: Response header missing information.
            MalformedResponse: Webex Teams API response did not contain expected data.

        Returns:
            File: Details about the file.
        """

        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Accept-Encoding": "", # ensures content-length always gets returned
        }

        r = requests.head(url, headers=headers)
        if r.status_code == 404:
            # Item must have been deleted since url was retrieved
            return File("", 0, "", "", True)

        r.raise_for_status()

        filename_re = re.search(r"filename=\"(.+?)\"", r.headers.get("Content-Disposition", ""), re.I)

        if not filename_re:
            message = (
                f"Failed to find filename='' in {r.headers.get('Content-Disposition', '')} for url {url}"
            )
            raise MalformedResponse(message)

        return File(r.headers.get("Content-Disposition", ""),
                    r.headers.get("Content-Length", 0),
                    r.headers.get("Content-Type", ""),
                    filename_re.group(1),
                    False)

    def archive_room(self, room_id: str, text_format: bool = True, html_format: bool = True,
                     json_format: bool = True, **options) -> str:
        """
        Archives a Webex Teams room. This creates a file called roomTitle_roomId.tgz 
        with the following contents:
        - roomTitle_roomId.txt - Text version of the conversations (if `text_format` is True)
        - roomTitle_roomId.html - HTML version of the conversations (if `html_format` is True)
        - files/ - Attachments added to the room (if `download_attachments` is True)

        Args:
            room_id: ID of the room to archive.
            text_format: Create a text version of the archive.
            html_format: Create an HTML version of the archive.
            json_format: Create a json version of the archive.

            Options:
                overwrite_folder: Overwrite the archive folder if it already exists.
                delete_folder: Delete the archive folder when done.
                reverse_order: Order messages by most recent on the bottom.
                download_attachments: Download attachments sent to the room.
                download_avatars: Download avatar images.
                download_workers: Number of download workers for downloading files.
                timestamp_format: Timestamp strftime format.

        Returns:
            Name of archive file.

        Raises:
            IOError: Error occurred while creating/writing to files.
            shutil.Error: Error occurred creating/copying/deleting files/folders.
            ValueError: Exception message will contain more details.
            TypeError: Messages contained non JSON serializable data.
            webexteamssdkException: An error occurred calling the Webex Teams API.
        """
        # Configure options
        overwrite_folder = options.get("overwrite_folder", True)
        delete_folder = options.get("delete_folder", False)
        reverse_order = options.get("reverse_order", True)
        download_attachments = options.get("download_attachments", True)
        download_avatars = options.get("download_avatars", True)
        download_workers = options.get("download_workers", 15)
        timestamp_format = options.get("timestamp_format", "%Y-%m-%dT%H:%M:%S")

        self._gather_room_information(room_id)

        # Prepare folder
        self._setup_folder(overwrite_folder, download_attachments, download_avatars, html_format)
        try:
            self._archive(reverse_order, download_attachments, download_avatars, download_workers,
                          text_format, html_format, json_format, timestamp_format)
            self._compress_folder()
        except Exception:
            self._tear_down_folder()
            raise

        if delete_folder:
            self._tear_down_folder()

        return f"{self.archive_folder_name}.tgz"

    def _archive(self, reverse_order: bool, download_attachments: bool,
                 download_avatars: bool, download_workers: int, text_format: bool,
                 html_format: bool, json_format: bool, timestamp_format: str) -> None:
        """
        Collects room messages and attachments using Webex Teams 
        APIs and writes them to text/html files.
        """
        # Structure: {"personEmail": webexteamssdk.PeopleAPI.Person}
        people = {}

        # Structure: {"url": File}
        attachments = {}

        # Structure: {"url": File}
        avatars = {}

        processed_messages = []

        # Variables used to keep track if message is start or additional
        repeat_indeces = []
        previous_person_email = ""
        previous_msg_datetime = None

        for index, msg in enumerate(self.messages):
            processed_messages.append(msg)

            if index > 0 and msg.personEmail == previous_person_email and \
               (previous_msg_datetime-msg.created).seconds < 60:
                repeat_indeces.append(index)

            previous_person_email = msg.personEmail
            previous_msg_datetime = msg.created

            if msg.personEmail not in people:
                try:
                    people[msg.personEmail] = self.sdk.people.get(msg.personId)
                except ApiError as e:
                    if e.response.status_code == 404:
                        people[msg.personEmail] = UserNotFound(
                            id=msg.personId,
                            emails=[msg.personEmail],
                            displayName="Person Not Found",
                            avatar=None,
                        )
                    else:
                        raise

                if download_avatars and people[msg.personEmail].avatar:
                    avatars[people[msg.personEmail].avatar] = File(
                        "", "", "", sanitize_name(msg.personEmail), False)

            if msg.files:
                for url in msg.files:
                    file_metadata = self.file_details(url)
                    attachments[url] = file_metadata

        if reverse_order:
            repeat_indeces = [len(processed_messages)-i for i in repeat_indeces]
            processed_messages = list(reversed(processed_messages))

        if html_format:
            self._create_html_transcript(processed_messages, attachments, people,
                                         download_avatars, repeat_indeces, timestamp_format)

        if text_format:
            self._create_text_transcript(processed_messages, attachments, timestamp_format)

        if download_attachments:
            self._download_files("attachments", attachments, download_workers)

        if download_avatars:
            self._download_files("avatars", avatars, download_workers)

        if json_format:
            self._create_json_transcript(processed_messages)

        # Write space information to json file
        with open(os.path.join(os.getcwd(), self.archive_folder_name, f"space_details.json"), "w", encoding="utf-8") as fh:
            space_details = {
                "space": self.room.to_dict(),
                "creator": self.room_creator._asdict() if isinstance(self.room_creator, UserNotFound) 
                                                       else self.room_creator.to_dict(),
            }
            json.dump(space_details, fh)

        logger.info("Room %s archived successfully.", self.room.id)

    def _setup_folder(self, overwrite_folder: bool, download_attachments: bool,
                      download_avatars, html_format: bool) -> None:
        """Creates a folder roomTitle_roomId to store archive."""

        if os.path.isdir(self.archive_folder_name) and overwrite_folder:
            shutil.rmtree(self.archive_folder_name, ignore_errors=False)

        os.makedirs(self.archive_folder_name)

        if download_attachments:
            os.makedirs(f"{self.archive_folder_name}/attachments")

        if download_avatars:
            os.makedirs(f"{self.archive_folder_name}/avatars")

        if html_format:
            basepath = os.path.dirname(os.path.realpath(__file__))

            shutil.copytree(f"{basepath}/static/.css", f"{self.archive_folder_name}/.css")
            shutil.copytree(f"{basepath}/static/.js", f"{self.archive_folder_name}/.js")
            shutil.copytree(f"{basepath}/static/.fonts", f"{self.archive_folder_name}/.fonts")

    def _tear_down_folder(self) -> None:
        """Deletes the roomTitle_roomId folder in case an exception was raised."""

        if os.path.isdir(self.archive_folder_name):
            shutil.rmtree(self.archive_folder_name, ignore_errors=False)

    def _gather_room_information(self, room_id: str) -> None:
        """Calls Webex Teams APIs to get room information and messages."""

        self.room = self.sdk.rooms.get(room_id)
        self.archive_folder_name = f"{sanitize_name(self.room.title)}_{room_id}"

        try:
            self.room_creator = self.sdk.people.get(self.room.creatorId)
        except ApiError as e:
            if e.response.status_code == 404:
                self.room_creator = UserNotFound(
                    id=self.room.creatorId,
                    emails=["unknown"],
                    displayName="Person Not Found",
                    avatar=None,
                )
            else:
                raise

        if self.sdk.people.me().type == "bot":
            self.messages = self.sdk.messages.list(room_id, mentionedPeople="me")
        else:
            self.messages = self.sdk.messages.list(room_id)

    def _create_text_transcript(self, messages: list, attachments: dict,
                                timestamp_format: str) -> None:
        """Writes room messages to a text file."""

        template = jinja_env.get_template("default.txt")
        text_transcript = template.render(
            room=self.room,
            room_creator=self.room_creator,
            messages=messages,
            attachments=attachments,
            timestamp_format=timestamp_format,
        )

        with open(os.path.join(os.getcwd(), self.archive_folder_name, f"{self.archive_folder_name}.txt"), "w", encoding="utf-8") as fh:
            fh.write(text_transcript)

    def _create_json_transcript(self, messages: list) -> None:
        """Writes room messages to a JSON file."""

        data = {
            "items": [m.to_dict() for m in messages]
        }
        with open(os.path.join(os.getcwd(), self.archive_folder_name, f"{self.archive_folder_name}.json"), "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def _create_html_transcript(self, messages: list, attachments: dict, people: dict,
                                download_avatars: dict, repeat_indeces: list,
                                timestamp_format: str) -> None:
        """Writes room messages to an HTML file."""

        template = jinja_env.get_template("default.html")
        html = template.render(
            room=self.room,
            room_creator=self.room_creator,
            messages=messages,
            attachments=attachments,
            people=people,
            download_avatars=download_avatars,
            repeat_indeces=repeat_indeces,
            timestamp_format=timestamp_format
        )

        with open(os.path.join(os.getcwd(), self.archive_folder_name, f"{self.archive_folder_name}.html"), "w", encoding="utf-8") as fh:
            fh.write(html)

    def _download_files(self, folder_name: str, links: dict, workers: int) -> None:
        """Downloads files given a list of URL links."""

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            result = {
                executor.submit(self._download_file, folder_name, url, links[url].filename): url for url in links
            }

            # Do this to check if any downloads failed.
            for future in concurrent.futures.as_completed(result):
                future.result()

    def _download_file(self, folder_name: str, url: str, filename: str) -> None:
        """Download file from Webex Teams."""

        headers = {
            "Authorization": f"Bearer {self.access_token}"
        }

        # https://stackoverflow.com/questions/16694907/how-to-download-
        # large-file-in-python-with-requests-py
        with requests.get(url, headers=headers, stream=True) as r:
            with open(os.path.join(os.getcwd(), self.archive_folder_name, folder_name, f"{filename}"), "wb") as f:
                shutil.copyfileobj(r.raw, f)

    def _compress_folder(self) -> None:
        """Compress `archive_folder_name` folder as `archive_folder_name`.tgz"""

        # https://stackoverflow.com/questions/2032403/how-to-create-
        # full-compressed-tar-file-using-python
        with tarfile.open(f"{self.archive_folder_name}.tgz", "w:gz") as tar:
            tar.add(self.archive_folder_name)
