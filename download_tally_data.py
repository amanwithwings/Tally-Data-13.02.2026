"""
Tally.xyz Data Export — Arbitrum DAO
=====================================
Downloads all governance data from the Tally.xyz GraphQL API and saves it
as both JSON and CSV files.

Based on the official Tally API docs at https://apidocs.tally.xyz/

Usage:
    pip install -r requirements.txt
    echo "TALLY_API_KEY=your_key_here" > .env
    python download_tally_data.py [--org arbitrum]

Output:
    output/json/*.json   — raw API responses
    output/csv/*.csv     — flattened tabular data
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

load_dotenv()

API_KEY = os.getenv("TALLY_API_KEY")
if not API_KEY:
    sys.exit("ERROR: TALLY_API_KEY not set. Add it to .env or export it.")

ENDPOINT = "https://api.tally.xyz/query"
HEADERS = {"Api-Key": API_KEY, "Content-Type": "application/json"}
PAGE_SIZE = 20
RATE_LIMIT_SLEEP = 1.1

OUT_JSON = Path("output/json")
OUT_CSV = Path("output/csv")
OUT_JSON.mkdir(parents=True, exist_ok=True)
OUT_CSV.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------

def gql_query(query: str, variables: dict, retries: int = 3) -> dict:
    """Execute a GraphQL query with retry + error body logging."""
    delay = 2
    for attempt in range(retries + 1):
        try:
            resp = requests.post(
                ENDPOINT,
                headers=HEADERS,
                json={"query": query, "variables": variables},
                timeout=30,
            )
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt < retries:
                    print(f"  [retry {attempt+1}/{retries}] HTTP {resp.status_code}, waiting {delay}s...")
                    time.sleep(delay)
                    delay *= 2
                    continue
                resp.raise_for_status()

            # For 422 errors, print the response body for debugging
            if resp.status_code == 422:
                print(f"  [422 error] Response body: {resp.text[:500]}")
                if attempt < retries:
                    print(f"  [retry {attempt+1}/{retries}] waiting {delay}s...")
                    time.sleep(delay)
                    delay *= 2
                    continue
                resp.raise_for_status()

            resp.raise_for_status()
            data = resp.json()
            if "errors" in data:
                print(f"  [GraphQL errors] {json.dumps(data['errors'], indent=2)[:500]}")
                raise ValueError(f"GraphQL errors: {data['errors']}")
            return data["data"]
        except requests.RequestException as e:
            if attempt < retries:
                print(f"  [retry {attempt+1}/{retries}] Network error: {e}, waiting {delay}s...")
                time.sleep(delay)
                delay *= 2
            else:
                raise
    return {}


def paginate(query: str, variables: dict, nodes_path: str) -> list:
    """Iterate through all pages of a paginated query."""
    all_nodes = []
    after_cursor = None
    page = 0

    while True:
        vars_copy = json.loads(json.dumps(variables))  # deep copy
        if "input" in vars_copy:
            if "page" not in vars_copy["input"]:
                vars_copy["input"]["page"] = {}
            vars_copy["input"]["page"]["limit"] = PAGE_SIZE
            if after_cursor:
                vars_copy["input"]["page"]["afterCursor"] = after_cursor

        data = gql_query(query, vars_copy)

        obj = data
        for key in nodes_path.split("."):
            obj = obj[key]

        nodes = obj.get("nodes", [])
        page_info = obj.get("pageInfo", {})

        all_nodes.extend(nodes)
        page += 1
        print(f"  page {page}: +{len(nodes)} records (total {len(all_nodes)})")

        last_cursor = page_info.get("lastCursor")
        if not nodes or not last_cursor or last_cursor == after_cursor:
            break

        after_cursor = last_cursor
        time.sleep(RATE_LIMIT_SLEEP)

    return all_nodes


def save_json(data, filename: str):
    path = OUT_JSON / filename
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)
    size_kb = path.stat().st_size / 1024
    print(f"  Saved {path} ({size_kb:.1f} KB)")


def save_csv(data: list, filename: str):
    if not data:
        print(f"  Skipping CSV {filename} (no data)")
        return
    path = OUT_CSV / filename
    df = pd.json_normalize(data, sep="_")
    df.to_csv(path, index=False)
    print(f"  Saved {path} ({len(df)} rows)")


# ---------------------------------------------------------------------------
# GraphQL queries — aligned with https://apidocs.tally.xyz/
# ---------------------------------------------------------------------------

# 1. Organization — simple single-object query
Q_ORG = """
query Organization($input: OrganizationInput!) {
  organization(input: $input) {
    id
    name
    slug
    chainIds
    tokenIds
    governorIds
    metadata {
      color
      description
      icon
    }
    hasActiveProposals
    proposalsCount
    delegatesCount
    delegatesVotesCount
    tokenOwnersCount
  }
}
"""

# 2. Governors — paginated, uses inline fragment on Governor
Q_GOVERNORS = """
query Governors($input: GovernorsInput!) {
  governors(input: $input) {
    nodes {
      ... on Governor {
        id
        chainId
        name
        slug
        type
        kind
        isPrimary
        quorum
        delegatesCount
        delegatesVotesCount
        tokenOwnersCount
        token {
          id
          name
          symbol
          supply
          decimals
        }
        proposalStats {
          total
          active
          failed
          passed
        }
        parameters {
          quorumVotes
          proposalThreshold
          votingDelay
          votingPeriod
          gracePeriod
          quorumNumerator
          quorumDenominator
          clockMode
        }
        metadata {
          description
        }
      }
    }
    pageInfo { firstCursor lastCursor count }
  }
}
"""

# 3. Proposals — paginated, uses inline fragment on Proposal
#    Fields match the Proposal type in the docs exactly
Q_PROPOSALS = """
query Proposals($input: ProposalsInput!) {
  proposals(input: $input) {
    nodes {
      ... on Proposal {
        id
        onchainId
        chainId
        status
        quorum
        block {
          number
          timestamp
        }
        start {
          ... on Block { number timestamp }
          ... on BlocklessTimestamp { timestamp }
        }
        end {
          ... on Block { number timestamp }
          ... on BlocklessTimestamp { timestamp }
        }
        metadata {
          title
          description
          eta
          ipfsHash
          discourseURL
          snapshotURL
          txHash
        }
        voteStats {
          type
          votesCount
          votersCount
          percent
        }
        proposer {
          address
          ens
          name
        }
        creator {
          address
          ens
          name
        }
        governor {
          id
          name
          slug
        }
        executableCalls {
          target
          value
          calldata
          signature
          chainId
          index
          type
        }
        events {
          type
          txHash
          createdAt
          block {
            number
            timestamp
          }
        }
      }
    }
    pageInfo { firstCursor lastCursor count }
  }
}
"""

# 4. Votes — paginated, uses inline fragment on OnchainVote
Q_VOTES = """
query Votes($input: VotesInput!) {
  votes(input: $input) {
    nodes {
      ... on OnchainVote {
        id
        amount
        type
        reason
        txHash
        isBridged
        block {
          number
          timestamp
        }
        voter {
          address
          ens
          name
        }
        proposal {
          id
          onchainId
          metadata {
            title
          }
        }
      }
    }
    pageInfo { firstCursor lastCursor count }
  }
}
"""

# 5. Delegates — paginated, uses inline fragment on Delegate
Q_DELEGATES = """
query Delegates($input: DelegatesInput!) {
  delegates(input: $input) {
    nodes {
      ... on Delegate {
        id
        votesCount
        delegatorsCount
        isPrioritized
        account {
          address
          ens
          name
          bio
          twitter
          picture
        }
        statement {
          statement
          statementSummary
          isSeekingDelegation
        }
        labels {
          id
          name
          shortName
          isFeatured
        }
        delegateEligibility {
          score
          type
          status
          updatedAt
        }
      }
    }
    pageInfo { firstCursor lastCursor count }
  }
}
"""

# 6. Delegators — paginated list of who delegates TO a given address
#    The API has "delegators" (who delegates to X) and "delegatees" (who X delegates to)
#    We fetch delegators for top delegates to build the delegation graph
Q_DELEGATORS = """
query Delegators($input: DelegationsInput!) {
  delegators(input: $input) {
    nodes {
      ... on Delegation {
        id
        blockNumber
        blockTimestamp
        chainId
        delegator {
          address
          ens
          name
        }
        delegate {
          address
          ens
          name
        }
        token {
          id
          symbol
          decimals
        }
        votes
      }
    }
    pageInfo { firstCursor lastCursor count }
  }
}
"""

# 7. Single token query (not paginated)
Q_TOKEN = """
query Token($input: TokenInput!) {
  token(input: $input) {
    id
    type
    name
    symbol
    supply
    decimals
    isIndexing
    isBehind
  }
}
"""


# ---------------------------------------------------------------------------
# Export functions
# ---------------------------------------------------------------------------

def export_organization(org_slug: str) -> dict:
    print(f"\n[1/7] Fetching organization: {org_slug}")
    data = gql_query(Q_ORG, {"input": {"slug": org_slug}})
    org = data.get("organization")
    if not org:
        sys.exit(f"ERROR: Organization '{org_slug}' not found on Tally.")
    save_json(org, "organization.json")
    print(f"  org id={org['id']} governors={len(org.get('governorIds') or [])} tokens={len(org.get('tokenIds') or [])}")
    return org


def export_governors(org: dict) -> list:
    print(f"\n[2/7] Fetching governors for org {org['id']}")
    governors = paginate(
        Q_GOVERNORS,
        {"input": {"filters": {"organizationId": org["id"]}}},
        "governors",
    )
    save_json(governors, "governors.json")
    save_csv(governors, "governors.csv")
    print(f"  Total governors: {len(governors)}")
    return governors


def export_proposals(org: dict) -> list:
    print(f"\n[3/7] Fetching proposals for org {org['id']}")
    proposals = paginate(
        Q_PROPOSALS,
        {"input": {"filters": {"organizationId": org["id"]}}},
        "proposals",
    )
    save_json(proposals, "proposals.json")
    save_csv(proposals, "proposals.csv")
    print(f"  Total proposals: {len(proposals)}")
    return proposals


def export_votes(proposals: list) -> list:
    print(f"\n[4/7] Fetching votes for {len(proposals)} proposals")
    all_votes = []
    for i, proposal in enumerate(proposals, 1):
        proposal_id = proposal.get("id")
        title = ""
        meta = proposal.get("metadata")
        if meta and isinstance(meta, dict):
            title = (meta.get("title") or "")[:60]
        print(f"  [{i}/{len(proposals)}] Proposal {proposal_id}: {title}")
        try:
            votes = paginate(
                Q_VOTES,
                {"input": {"filters": {"proposalId": str(proposal_id)}}},
                "votes",
            )
            # Tag each vote with the proposal id for easier joining later
            for v in votes:
                v["_proposal_id"] = proposal_id
                v["_proposal_title"] = title
            all_votes.extend(votes)
        except Exception as e:
            print(f"  WARNING: Failed to fetch votes for proposal {proposal_id}: {e}")
            continue

    save_json(all_votes, "votes.json")
    save_csv(all_votes, "votes.csv")
    print(f"  Total votes: {len(all_votes)}")
    return all_votes


def export_delegates(org: dict) -> list:
    print(f"\n[5/7] Fetching delegates for org {org['id']}")
    delegates = paginate(
        Q_DELEGATES,
        {"input": {"filters": {"organizationId": org["id"]}}},
        "delegates",
    )
    save_json(delegates, "delegates.json")
    save_csv(delegates, "delegates.csv")
    print(f"  Total delegates: {len(delegates)}")
    return delegates


def export_delegations(org: dict, delegates: list) -> list:
    """
    Fetch delegator relationships for the top delegates.
    The Tally API doesn't have a bulk 'delegations' query — you query
    delegators per address. We fetch for top 50 delegates by votesCount.
    """
    print(f"\n[6/7] Fetching delegations (top delegate relationships)")

    # Sort delegates by votesCount descending, take top 50
    sorted_delegates = sorted(
        delegates,
        key=lambda d: int(d.get("votesCount") or 0),
        reverse=True,
    )
    top_delegates = sorted_delegates[:50]

    all_delegations = []
    for i, delegate in enumerate(top_delegates, 1):
        addr = delegate.get("account", {}).get("address", "")
        name = delegate.get("account", {}).get("name", "") or delegate.get("account", {}).get("ens", "")
        votes = delegate.get("votesCount", "?")
        print(f"  [{i}/{len(top_delegates)}] {name or addr[:12]}... (votes: {votes})")

        if not addr:
            continue

        try:
            delegators = paginate(
                Q_DELEGATORS,
                {"input": {"filters": {"address": addr, "organizationId": org["id"]}}},
                "delegators",
            )
            for d in delegators:
                d["_delegate_address"] = addr
                d["_delegate_name"] = name
            all_delegations.extend(delegators)
        except Exception as e:
            print(f"  WARNING: Failed for {addr}: {e}")
            continue

    save_json(all_delegations, "delegations.json")
    save_csv(all_delegations, "delegations.csv")
    print(f"  Total delegation relationships: {len(all_delegations)}")
    return all_delegations


def export_tokens(org: dict) -> list:
    """Fetch token info using the single-token query for each tokenId."""
    print(f"\n[7/7] Fetching token info")
    token_ids = org.get("tokenIds", [])
    if not token_ids:
        print("  No tokens found.")
        save_json([], "tokens.json")
        return []

    tokens = []
    for tid in token_ids:
        print(f"  Fetching token: {tid}")
        try:
            data = gql_query(Q_TOKEN, {"input": {"id": tid}})
            token = data.get("token")
            if token:
                tokens.append(token)
        except Exception as e:
            print(f"  WARNING: Failed to fetch token {tid}: {e}")

    save_json(tokens, "tokens.json")
    print(f"  Total tokens: {len(tokens)}")
    return tokens


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Export all Tally.xyz governance data")
    parser.add_argument("--org", default="arbitrum", help="Tally org slug (default: arbitrum)")
    args = parser.parse_args()

    print("=" * 60)
    print("Tally.xyz Data Export")
    print(f"Org: {args.org}")
    print(f"Output: {Path('output').resolve()}/")
    print("=" * 60)

    start = time.time()

    org = export_organization(args.org)
    governors = export_governors(org)
    proposals = export_proposals(org)
    votes = export_votes(proposals)
    delegates = export_delegates(org)
    delegations = export_delegations(org, delegates)
    tokens = export_tokens(org)

    elapsed = time.time() - start
    print("\n" + "=" * 60)
    print("Export complete!")
    print(f"  Governors:   {len(governors)}")
    print(f"  Proposals:   {len(proposals)}")
    print(f"  Votes:       {len(votes)}")
    print(f"  Delegates:   {len(delegates)}")
    print(f"  Delegations: {len(delegations)}")
    print(f"  Tokens:      {len(tokens)}")
    print(f"  Time:        {elapsed/60:.1f} minutes")
    print(f"  JSON output: {OUT_JSON.resolve()}/")
    print(f"  CSV output:  {OUT_CSV.resolve()}/")
    print("=" * 60)
    print("\nIMPORTANT: Back up the output/ directory to a safe location!")


if __name__ == "__main__":
    main()
