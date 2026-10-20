// Pinned GraphQL wire contracts, shared by native transports.
pub const BOARD___INIT___0: &str = r###"query($owner:String!, $name:String!) {
              repository(owner:$owner, name:$name) { projectsV2(first:100) {
                nodes { id number title closed url owner { ... on User { login } ... on Organization { login } } }
              } }
            }"###;
pub const BOARD_ENSURE_REVIEW_STATUS_0: &str = r###"query($id:ID!) { node(id:$id) {
          ... on ProjectV2SingleSelectField { options { id name color description } }
        } }"###;
pub const BOARD_ENSURE_REVIEW_STATUS_1: &str = r###"mutation($input:UpdateProjectV2FieldInput!) {
          updateProjectV2Field(input:$input) { projectV2Field { ... on ProjectV2SingleSelectField { id } } }
        }"###;
pub const BOARD_LINKED_PRS_0: &str = r###"query($id:ID!, $cursor:String!) {
              node(id:$id) { ... on ProjectV2Item {
                links: fieldValueByName(name:"Linked pull requests") {
                  ... on ProjectV2ItemFieldPullRequestValue {
                    pullRequests(first:100, after:$cursor) {
                      nodes { url } pageInfo { hasNextPage endCursor }
                    }
                  }
                }
              } }
            }"###;
pub const BOARD_ITEMS_0: &str = r###"query($id:ID!, $cursor:String) {
              node(id:$id) { ... on ProjectV2 { items(first:100, after:$cursor) {
                totalCount pageInfo { hasNextPage endCursor }
                nodes { id fieldValueByName(name:"Status") {
                  ... on ProjectV2ItemFieldSingleSelectValue { name }
                } links: fieldValueByName(name:"Linked pull requests") {
                  ... on ProjectV2ItemFieldPullRequestValue {
                    pullRequests(first:10) { nodes { url } pageInfo { hasNextPage endCursor } }
                  }
                } content { ... on Issue {
                  number title body url state repository { nameWithOwner }
                  issueDependenciesSummary { blockedBy }
                } } }
              } } }
            }"###;
pub const BOARD_DEPENDENCIES_READY_0: &str = r###"query($owner:String!, $name:String!, $number:Int!) {
          repository(owner:$owner, name:$name) { issue(number:$number) {
            issueDependenciesSummary { blockedBy }
          } }
        }"###;
pub const BOARD_ITEM_STATUS_0: &str = r###"query($id:ID!) { node(id:$id) { ... on ProjectV2Item {
          project { id } fieldValueByName(name:"Status") {
            ... on ProjectV2ItemFieldSingleSelectValue { name }
          }
        } } }"###;
pub const RUNNER_DEPENDENCIES_READY_0: &str = r###"query($owner:String!, $name:String!, $number:Int!) {
          repository(owner:$owner, name:$name) { issue(number:$number) {
            issueDependenciesSummary { blockedBy }
          } }
        }"###;
pub const RUNNER_REVIEW_THREADS_0: &str = r###"query($owner:String!, $name:String!, $number:Int!, $cursor:String) {
              repository(owner:$owner, name:$name) { pullRequest(number:$number) {
                reviewThreads(first:100, after:$cursor) {
                  nodes { id isResolved isOutdated viewerCanResolve path line
                    comments(first:100) {
                      nodes { id body url author { login } }
                      pageInfo { hasNextPage endCursor }
                    }
                  }
                  pageInfo { hasNextPage endCursor }
                }
              } }
            }"###;
pub const RUNNER_REVIEW_THREADS_1: &str = r###"query($id:ID!, $cursor:String!) {
                      node(id:$id) { ... on PullRequestReviewThread {
                        comments(first:100, after:$cursor) {
                          nodes { id body url author { login } }
                          pageInfo { hasNextPage endCursor }
                        }
                      } }
                    }"###;
