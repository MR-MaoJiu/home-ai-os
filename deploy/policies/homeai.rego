package homeai
import rego.v1

default allow := false
allow if {
    input.actor.user_id != ""
    input.actor.household_id != ""
    input.risk < 3
}
allow if {
    input.actor.user_id != ""
    input.actor.household_id != ""
    input.risk == 3
    input.approved == true
}
