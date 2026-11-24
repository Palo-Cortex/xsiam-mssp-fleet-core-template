Frozen snapshot of the template's `fleet/` (and its MSSP catalog) as shipped.
The setup-wizard tests build every scratch repo from THIS, not from the repo's
current `fleet/`, so they behave the same in the template and in a customer
fleet that has already been set up. Refresh it only when the template's
shipped examples change.
