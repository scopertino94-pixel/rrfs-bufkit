#!/usr/local/bin/perl
## WW Bufkit RRFS.pl  -  RRFS BUFKIT profile downloader
##
## Pulls pre-built RRFS .buf profiles from a public Hugging Face dataset and
## drops them into the local BUFKIT Data folder.  No Python and no decoding on
## this machine: the profiles are produced centrally and just downloaded here,
## exactly like the PSU scripts (HTTPS via curl instead of FTP).
##
## One-time: run "Setup RRFS in BUFKIT.pl" once to add RRFS to the BUFKIT menu.
## Schedule this like the other WW scripts (00/06/12/18Z cycles).

use strict;
use warnings;

my $REPO = "ORG/rrfs-bufkit";   # set ORG to the host (e.g. a Hugging Face dataset) serving the .buf files
my $BASE = "https://huggingface.co/datasets/$REPO/resolve/main";
my $OUT  = "C:/Program Files (x86)/BUFKIT/Data";

my @sites = (
    "3ck","2is","agr","aqq","atlh","atl4","kack","kabe","kacy","kafj","kagc","kalb",
    "kaoo","kaug","kavp","kbaf","kbfm","kbdl","bid","kbdr","kbed","b#q","b#v","b#x",
    "b#w","kbgr","kbmg","kbos","kbvi","kbvy","kbwi","kchh","c09","can","kcef","kcgx",
    "coat","kcho","kcmh","kcon","cty","kcvg","kday","kdca","kdkb","kdpa","dov","kecg",
    "kenw","keri","evr","kewb","kewr","kezf","kfdy","kfmh","kfmy","kgfl","grun","kgon",
    "kgnv","kgyy","khat","khfd","hgr","khpn","khuf","khvn","khya","kiad","ijd","kilg",
    "kind","kisp","kipt","kith","kjax","kjfk","kjvl","liso","klaf","klbe","klck","lm3",
    "klns","klga","klot","kluk","klwm","kmco","kmdt","kmdw","kmht","kmia","mie","kmiv",
    "kmke","kmlb","kmmu","kmob","kmpo","kmsv","kmtn","kmlb","nhk","koqu","kord","korf",
    "korh","okx","kore","kpbi","kphl","kphf","kpie","kpit","kpne","kpns","kpou","kpvd",
    "kpwm","kpsm","kpym","rutg","krdg","krfd","kric","kroa","krnk","krpj","ksby","ksch",
    "spa","kswf","ksyr","ksfm","kteb","ktpa","kttn","tmsr","tow","kugn","kunv","kvrb",
    "w54","woo","wtby","kwwd","xmr","kpia","kgbg","kcmi","kpnt","kijx","kbmi","kdnv",
    "kpah","ktaz","kstl","kmdh","klwv","kdec","kuin","kspi","koly","khuf","kc75","txkf"
);

print "Downloading the latest RRFS BUFKIT profiles from server - please wait\n\n";

my ($ok, $miss) = (0, 0);
foreach my $site (@sites) {
    (my $u = $site) =~ s/#/%23/g;            # '#' is illegal in a URL path
    my $url = "$BASE/rrfs_${u}.buf";
    my $dst = "$OUT/rrfs_${site}.buf";
    # list-form system() => no shell quoting issues with spaces or '#'
    my $rc = system("curl", "--ssl-no-revoke", "-f", "-s", "-S", "-L", "-o", $dst, $url);
    if ($rc == 0) { print "Successfully downloaded $site\n"; $ok++; }
    else          { print "  (skipped $site - not available this cycle)\n"; $miss++; }
}

print "\nDone.  Downloaded: $ok   Skipped: $miss\n";
