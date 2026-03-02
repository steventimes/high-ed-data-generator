use anyhow::{Context, Result};

#[derive(Clone, Copy, Debug)]
pub enum TermSeason {
    SP,
    SU,
    FA,
}

#[derive(Clone, Copy, Debug)]
pub struct Term {
    pub year: i32,
    pub season: TermSeason,
}

impl Term {
    pub fn code(&self) -> String {
        let s = match self.season {
            TermSeason::SP => "SP",
            TermSeason::SU => "SU",
            TermSeason::FA => "FA",
        };
        format!("{}{}", self.year, s)
    }

    pub fn next(&self) -> Term {
        match self.season {
            TermSeason::FA => Term {
                year: self.year + 1,
                season: TermSeason::SP,
            },
            TermSeason::SP => Term {
                year: self.year,
                season: TermSeason::SU,
            },
            TermSeason::SU => Term {
                year: self.year,
                season: TermSeason::FA,
            },
        }
    }
}

pub fn parse_term_code(s: &str) -> Result<Term> {
    if s.len() != 6 {
        anyhow::bail!("term code must look like 2025FA / 2025SP / 2026SU");
    }
    let year: i32 = s[0..4].parse().context("failed to parse year")?;
    let season = match &s[4..6] {
        "FA" => TermSeason::FA,
        "SP" => TermSeason::SP,
        "SU" => TermSeason::SU,
        other => anyhow::bail!("unsupported season {other} (use FA/SP/SU)"),
    };
    Ok(Term { year, season })
}

pub fn build_term_sequence(start_term: &str, count: usize) -> Result<Vec<Term>> {
    let mut terms = Vec::with_capacity(count);
    let mut current = parse_term_code(start_term)?;
    for _ in 0..count {
        terms.push(current);
        current = current.next();
    }
    Ok(terms)
}