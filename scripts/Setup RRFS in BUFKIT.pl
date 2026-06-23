#!/usr/local/bin/perl
## Setup RRFS in BUFKIT.pl  -  run ONCE per machine.
##
## Adds a single "RRFS          eta" line to the BUFKIT model list in
## Bufkit.cfg so the RRFS profiles show up in the model menu.  Safe to run
## more than once: if RRFS is already present it does nothing.  It only ever
## inserts one line after the GFS3 line and never touches the profile
## directory settings.

use strict;
use warnings;

my $cfg = "C:/Users/Public/Bufkit/Bufkit.cfg";
die "Cannot find Bufkit.cfg at $cfg\n" unless -f $cfg;

open my $in, "<", $cfg or die "Cannot read $cfg: $!\n";
binmode $in;
my @lines = <$in>;
close $in;

if (grep { /^RRFS\s/i } @lines) {
    print "RRFS is already in the BUFKIT model list - nothing to do.\n";
    exit 0;
}

my @out;
my $inserted = 0;
foreach my $l (@lines) {
    push @out, $l;
    if (!$inserted && $l =~ /^GFS3\s/i) {
        push @out, "RRFS          eta\r\n";
        $inserted = 1;
    }
}

die "Could not find the GFS3 model line - Bufkit.cfg left unchanged.\n"
    unless $inserted;

open my $w, ">", $cfg or die "Cannot write $cfg: $!\n";
binmode $w;
print $w @out;
close $w;

print "RRFS added to the BUFKIT model list.\n";
print "Close and reopen BUFKIT, then run 'WW Bufkit RRFS.pl' to fetch profiles.\n";
