from datetime import datetime, timezone
import tomli
from webexteamsarchiver import WebexTeamsArchiver

with open("date-config.toml", "rb") as f:
    toml_dict = tomli.load(f)

personal_token = toml_dict['creds']['token']
user_defined_time = toml_dict['time']['start']
start_datetime = datetime.strptime(user_defined_time, '%m-%d-%Y %H:%M')
archiver = WebexTeamsArchiver(personal_token)

for channel in toml_dict['channels'].values():
    archiver.archive_room(channel, json_format=False, text_format=False, compress_folder=False, file_format=zip, start_date=start_datetime)