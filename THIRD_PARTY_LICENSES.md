# Third-party licenses

## Playwright (Microsoft Corporation)

`src/repld/browser/injected_source.py` contains a bundled build of Playwright's
browser-side `InjectedScript` engine — `packages/injected/src/injectedScript.ts`
and its `packages/isomorphic/` dependencies — from
<https://github.com/microsoft/playwright>, pinned to the commit recorded in that
module's `COMMIT` constant. It is regenerated (never hand-edited) by
`scripts/build_injected.py` via `make injected`.

Playwright is licensed under the Apache License, Version 2.0:

> Copyright (c) Microsoft Corporation.
>
> Licensed under the Apache License, Version 2.0 (the "License");
> you may not use this file except in compliance with the License.
> You may obtain a copy of the License at
>
> http://www.apache.org/licenses/LICENSE-2.0
>
> Unless required by applicable law or agreed to in writing, software
> distributed under the License is distributed on an "AS IS" BASIS,
> WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
> See the License for the specific language governing permissions and
> limitations under the License.

The full license text is available at
<https://github.com/microsoft/playwright/blob/main/LICENSE>.
