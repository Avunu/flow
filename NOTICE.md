# Notices

Flow is licensed under the [GNU AGPLv3](license.txt).

## Frappe Technologies Pvt. Ltd. (MIT)

Parts of Flow are ported from Flow in the Frappe framework, `frappe/flow` in
<https://github.com/frappe/frappe>, which is released under the MIT licence. The ported pieces are
adapted to this repository's namespace and structure. They are:

- the field-level read permission on `get_doc` and `get_last_doc`, and the email attachment checks,
  in the `execute` sandbox (`flow/utils/safe_exec.py`).

The copyright and permission notice for those pieces follows.

```
MIT License

Copyright (c) 2026 Frappe Technologies Pvt. Ltd.

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
```
