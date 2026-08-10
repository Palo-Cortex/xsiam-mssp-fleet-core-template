Echoes a message back as structured context output. Minimal exemplar of an
MSSP-authored script shipped through the fleet's release, pin, and converge
pipeline — replace with your own content when adopting the template.

## Script Data

| **Name** | **Description** |
| --- | --- |
| Script Type | python3 |
| Tags | |

## Inputs

| **Argument Name** | **Description** |
| --- | --- |
| message | The message to echo back into context. |
| uppercase | Whether to upper-case the message before returning it. Default is false. |

## Outputs

| **Path** | **Description** | **Type** |
| --- | --- | --- |
| ExampleScript.Message | The echoed message. | String |

## Example

```
!ExampleScript message="hello fleet" uppercase=true
```
