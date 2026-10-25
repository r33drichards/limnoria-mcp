# Example script data only. No Python, host commands, filesystem, or networking.
if {[context_get text] eq "!hello"} {
    reply "Hello [context_get nick]"
}
