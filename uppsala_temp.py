# Copyright (c) 2026 MyCompany LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import urllib.request

def main():
    url = "https://api.open-meteo.com/v1/forecast?latitude=59.8588&longitude=17.6389&current=temperature_2m"
    try:
        with urllib.request.urlopen(url) as response:
            data = json.loads(response.read().decode())
            temp = data["current"]["temperature_2m"]
            unit = data["current_units"]["temperature_2m"]
            print(f"The current temperature in Uppsala, Sweden is {temp}{unit}")
    except Exception as e:
        print(f"Failed to fetch temperature: {e}")

if __name__ == "__main__":
    main()
