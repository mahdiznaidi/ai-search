# Version corrigée du StageFinder - Principales améliorations
import pandas as pd
from datetime import datetime
import time
import os
import requests
import json
import PyPDF2
from io import BytesIO
from jobspy import scrape_jobs
#from jobs import scrape_jobs
import logging
from typing import Optional, Dict, List, Union
import requests
from bs4 import BeautifulSoup

# Configuration du logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration API - Modifiez votre clé ici
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
CV_PATH = "C:\\Users\\Mehdi\\Desktop\\AI search\\cv_mehdi_znaidi.pdf"   # Modifiez le chemin vers votre CV si vous en avez un

class StageFinder:
    def __init__(self, hours_old: int = 168, use_ai_filtering: bool = False, 
                 openrouter_api_key: Optional[str] = None, cv_path: Optional[str] = None):
        """
        Initialise le chercheur de stages
        
        Args:
            hours_old: Période en heures pour filtrer les publications récentes
            use_ai_filtering: Utiliser le filtrage IA intelligent
            openrouter_api_key: Clé API OpenRouter pour GPT-4/o1
            cv_path: Chemin vers le CV PDF pour matching personnalisé
        """
        self.hours_old = hours_old
        self.use_ai_filtering = use_ai_filtering
        self.openrouter_api_key = openrouter_api_key
        self.cv_path = cv_path
        self.cv_content = None
        self.user_profile = None
        
        # Configuration IA
        self.ai_model = "openai/o4-mini"
        self.ai_endpoint = "https://openrouter.ai/api/v1/chat/completions"
        
        # Configuration retry pour API calls
        self.max_retries = 3
        self.retry_delay = 2
        
        # Charger et analyser le CV si fourni
        if self._validate_cv_setup():
            logger.info(f"✅ CV analysé: {cv_path}")
            if self.user_profile:
                logger.info(f"🎯 Profil détecté: {self.user_profile.get('specialization', 'Généraliste Data Science')}")
        
        self.publication_config = {
            24: "24 dernières heures",
            72: "3 derniers jours", 
            168: "dernière semaine",
            336: "2 dernières semaines",
            720: "dernier mois",
            1440: "2 derniers mois",
            None: "toutes périodes"
        }
        
        # Mots-clés optimisés
        self.domain_keywords = {
            "data_science": [
                "stage data science", "Data Science Intern", "stagiaire data science",
                "stage data scientist", "stage analyse de données", "data science internship"
            ],
            "machine_learning": [
                "stage machine learning", "stage ML", "intern ML", "machine learning intern",
                "stagiaire ML", "stage apprentissage automatique", "ML internship"
            ],
            "intelligence_artificielle": [
                "stage IA", "stage AI", "stage intelligence artificielle",
                "artificial intelligence intern", "stagiaire IA", "AI internship"
            ],
            "deep_learning": [
                "stage deep learning", "stage neural networks", "deep learning intern",
                "stagiaire deep learning", "stage réseaux de neurones", "DL internship"
            ]
        }
        
        # Villes françaises prioritaires
        self.locations = [
            "Paris, France", "Lyon, France", "Toulouse, France", "Marseille, France",
            "Bordeaux, France", "Lille, France", "Nantes, France", "Strasbourg, France",
            "Montpellier, France", "Nice, France", "Rennes, France", "Grenoble, France", "France"
        ]
        
        self.sites = ["indeed", "linkedin"]

        

    def scrape_jobteaser(self, keyword: str, location: str, max_results: int = 20) -> pd.DataFrame:
            """Scrape JobTeaser pour les stages"""
            try:
                url = f"https://www.jobteaser.com/fr/job-offers?query={keyword}&location={location}"
                headers = {"User-Agent": "Mozilla/5.0"}
                response = requests.get(url, headers=headers, timeout=15)

                if response.status_code != 200:
                    logger.warning(f"JobTeaser: status {response.status_code}")
                    return pd.DataFrame()

                soup = BeautifulSoup(response.text, 'html.parser')
                jobs = []

                for offer in soup.select('.job-offer-card')[:max_results]:
                    try:
                        title = offer.select_one('.job-offer-card__title').get_text(strip=True)
                        company = offer.select_one('.job-offer-card__company').get_text(strip=True)
                        job_url = "https://www.jobteaser.com" + offer.select_one('a')['href']

                        jobs.append({
                            'title': title,
                            'company': company,
                            'location': location,
                            'job_url': job_url,
                            'description': '',
                            'date_posted': datetime.now()
                        })
                    except Exception as e:
                        logger.debug(f"Erreur parsing offre JobTeaser: {e}")
                        continue

                return pd.DataFrame(jobs)
            except Exception as e:
                logger.error(f"Erreur scraping JobTeaser: {e}")
                return pd.DataFrame()

    def _scrape_jobs_safe(self, keyword: str, location: str, max_results: int) -> Optional[pd.DataFrame]:
        """Scraping sécurisé avec gestion d'erreur + JobTeaser"""
        try:
            # ✅ Scraping via JobSpy (Indeed + LinkedIn)
            jobspy_df = scrape_jobs(
                site_name=self.sites,
                search_term=keyword,
                location=location,
                results_wanted=max_results,
                hours_old=self.hours_old,
                country_indeed="france",
                job_type="internship" if "indeed" in self.sites else None
            )

            # ✅ Ajout JobTeaser si activé
            if self.enable_jobteaser:
                jobteaser_df = self.scrape_jobteaser(keyword, location, max_results)
                if not jobteaser_df.empty:
                    logger.info(f"JobTeaser: {len(jobteaser_df)} offres ajoutées")
                    jobspy_df = pd.concat([jobspy_df, jobteaser_df], ignore_index=True)

            return jobspy_df
        except Exception as e:
            logger.error(f"Erreur scraping: {e}")
            return None  
    
    def _validate_cv_setup(self) -> bool:
        """Valide et configure le CV si fourni"""
        if not (self.cv_path and os.path.exists(self.cv_path) and self.openrouter_api_key):
            return False
            
        try:
            self.cv_content = self._extract_cv_content(self.cv_path)
            if self.cv_content:
                self.user_profile = self._analyze_cv_with_ai(self.cv_content)
                return True
        except Exception as e:
            logger.error(f"Erreur validation CV: {e}")
            
        return False
    
    def _make_api_call(self, data: Dict) -> Optional[Dict]:
        """Effectue un appel API avec retry logic"""
        headers = {
            "Authorization": f"Bearer {self.openrouter_api_key}",
            "Content-Type": "application/json"
        }
        
        for attempt in range(self.max_retries):
            try:
                response = requests.post(
                    self.ai_endpoint, 
                    headers=headers, 
                    json=data, 
                    timeout=30
                )
                
                if response.status_code == 200:
                    return response.json()
                elif response.status_code == 429:  # Rate limit
                    wait_time = self.retry_delay * (2 ** attempt)
                    logger.warning(f"Rate limit atteint, attente {wait_time}s...")
                    time.sleep(wait_time)
                    continue
                else:
                    logger.error(f"Erreur API: {response.status_code}")
                    
            except requests.exceptions.RequestException as e:
                logger.error(f"Erreur réseau (tentative {attempt + 1}): {e}")
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_delay)
                    
        return None
    
    def search_stages(self, max_results_per_search: int = 20, delay: int = 2) -> tuple:
        """
        Recherche intelligente de stages avec rotation des mots-clés et villes
        """
        all_jobs = []
        search_summary = []
        
        period_desc = self.publication_config.get(self.hours_old, f"{self.hours_old}h")
        logger.info(f"🔍 Démarrage recherche Data Science - Période: {period_desc}")
        logger.info(f"📊 {len(self.domain_keywords)} domaines × {len(self.locations)} villes")
        
        total_searches = sum(len(keywords[:2]) for keywords in self.domain_keywords.values()) * min(8, len(self.locations))
        current_search = 0
        
        for domain, keywords in self.domain_keywords.items():
            logger.info(f"\n🎯 Domaine: {domain.replace('_', ' ').title()}")
            
            for location in self.locations[:8]:  # Limiter à 8 villes principales
                primary_keywords = keywords[:8]  # 2 mots-clés principaux
                
                for keyword in primary_keywords:
                    current_search += 1
                    progress = f"[{current_search}/{total_searches}]"
                    
                    try:
                        logger.info(f"  {progress} 🔎 '{keyword}' à {location}")
                        
                        # Recherche avec gestion d'erreur
                        jobs = self._scrape_jobs_safe(keyword, location, max_results_per_search)
                        
                        if jobs is not None and not jobs.empty:
                            # Filtrage intelligent
                            filtered_jobs = self._apply_filtering(jobs)
                            
                            if not filtered_jobs.empty:
                                # Ajouter métadonnées
                                filtered_jobs = self._add_metadata(filtered_jobs, domain, keyword, location)
                                all_jobs.append(filtered_jobs)
                                
                                count = len(filtered_jobs)
                                excluded = len(jobs) - count
                                logger.info(f"    ✅ {count} stages trouvés ({excluded} exclus)")
                                
                                search_summary.append({
                                    'domaine': domain,
                                    'location': location, 
                                    'keyword': keyword,
                                    'results': count,
                                    'excluded': excluded,
                                    'periode_recherche': period_desc
                                })
                            else:
                                logger.info("    ❌ Aucun stage après filtrage")
                        else:
                            logger.info("    ❌ Aucun résultat")
                            
                        # Délai pour éviter les blocages
                        time.sleep(delay)
                        
                    except Exception as e:
                        logger.error(f"    ⚠️ Erreur: {str(e)}")
                        continue
        
        return all_jobs, search_summary
    
    def _scrape_jobs_safe(self, keyword: str, location: str, max_results: int) -> Optional[pd.DataFrame]:
        """Scraping sécurisé avec gestion d'erreur"""
        try:
            return scrape_jobs(
                site_name=self.sites,
                search_term=keyword,
                location=location,
                results_wanted=max_results,
                hours_old=self.hours_old,
                country_indeed="france",
                job_type="internship" if "indeed" in self.sites else None
            )
        except Exception as e:
            logger.error(f"Erreur scraping: {e}")
            return None
    
    def _apply_filtering(self, jobs_df: pd.DataFrame) -> pd.DataFrame:
        """Applique le filtrage approprié selon la configuration"""
        if self.use_ai_filtering and self.openrouter_api_key:
            if self.user_profile:
                return self.ai_match_with_profile(jobs_df)
            else:
                return self.ai_filter_internships(jobs_df)
        else:
            return self.filter_for_internships(jobs_df)
    
    def _add_metadata(self, df: pd.DataFrame, domain: str, keyword: str, location: str) -> pd.DataFrame:
        """Ajoute les métadonnées de recherche"""
        df = df.copy()
        df['domaine_recherche'] = domain
        df['mot_cle_utilise'] = keyword
        df['ville_recherche'] = location
        df['timestamp_recherche'] = datetime.now()
        return df
    
    def filter_for_internships(self, jobs_df: pd.DataFrame) -> pd.DataFrame:
        """Filtre AMÉLIORÉ et plus flexible pour identifier les stages DATA"""
        if jobs_df.empty:
            return jobs_df
        
        # ✅ Indicateurs de stages - ÉLARGI
        stage_indicators = [
            'stage', 'stagiaire', 'intern', 'internship', 'étudiant', 'student',
            'fin d\'études', 'pfe', 'master', '6 mois', 'graduate', 'apprentice',
            'trainee', 'apprentissage', 'jeune diplômé', 'junior'  # ✅ Ajouté
        ]
        
        # ✅ Exclusions - PLUS INTELLIGENTES (contextuelles)
        strong_exclude_patterns = [
            'cdi temps plein', 'cdd temps plein', 'permanent position', 
            'senior manager', 'lead developer', 'chef d\'équipe',
            'directeur', 'responsable équipe', '5+ années d\'expérience'
        ]
        
        # ✅ Mots-clés DATA - PLUS FLEXIBLES
        data_keywords_flexible = [
            # Termes techniques
            "data scientist", "data science", "data analyst", "machine learning",
            "deep learning", "intelligence artificielle", "neural network", 
            "computer vision", "nlp", "big data", "data mining",
            
            # Termes plus généraux - ✅ AJOUTÉ
            "analyse de données", "science des données", "statistiques",
            "business intelligence", "analytique", "données", "algorithme",
            "python", "r programming", "sql", "tableau", "power bi",
            "statistique", "prédictif", "modélisation", "visualisation",
            
            # Domaines d'application - ✅ NOUVEAU
            "data engineer", "data product", "analytics", "reporting",
            "dashboard", "kpi", "metrics", "insights", "data driven"
        ]
        
        # ✅ Domaines connexes acceptés - NOUVEAU
        related_domains = [
            "informatique", "mathématiques", "statistiques", "économétrie",
            "finance quantitative", "recherche opérationnelle", "actuariat"
        ]
        
        def is_flexible_data_internship(row) -> bool:
            title = str(row.get('title', '')).lower()
            description = str(row.get('description', '')).lower()
            text_to_check = f"{title} {description}"
            
            # 1. ✅ DOIT être un stage (critère maintenu)
            has_stage_keyword = any(keyword in text_to_check for keyword in stage_indicators)
            if not has_stage_keyword:
                return False
            
            # 2. ✅ EXCLUSIONS plus intelligentes (patterns complets)
            has_strong_exclude = any(pattern in text_to_check for pattern in strong_exclude_patterns)
            if has_strong_exclude:
                return False
            
            # 3. ✅ CRITÈRE DATA ASSOUPLI - Au moins UN des critères
            has_data_keywords = any(keyword in text_to_check for keyword in data_keywords_flexible)
            has_related_domain = any(domain in text_to_check for domain in related_domains)
            has_technical_tools = any(tool in text_to_check for tool in ['python', 'r ', 'sql', 'matlab', 'spss', 'sas'])
            
            # ✅ NOUVEAU : Critère flexible (OR au lieu de AND strict)
            data_related = has_data_keywords or has_related_domain or has_technical_tools
            
            # 4. ✅ BONUS : Mentions de formations DATA
            has_data_education = any(edu in text_to_check for edu in [
                'master data', 'école d\'ingénieur', 'mathématiques appliquées',
                'statistiques', 'informatique décisionnelle'
            ])
            
            return data_related or has_data_education
        
        # Appliquer le filtre flexible
        mask = jobs_df.apply(is_flexible_data_internship, axis=1)
        filtered_df = jobs_df[mask].copy()
        
        return filtered_df.sort_values('date_posted', ascending=False) if not filtered_df.empty else filtered_df

    
    def ai_filter_internships(self, jobs_df: pd.DataFrame) -> pd.DataFrame:
        """Filtrage IA intelligent"""
        if jobs_df.empty or not self.openrouter_api_key:
            logger.warning("Pas d'API key, utilisation du filtrage classique")
            return self.filter_for_internships(jobs_df)
        
        logger.info(f"🤖 Analyse IA de {len(jobs_df)} offres...")
        
        # Filtrage de base d'abord
        basic_filtered = self._basic_internship_filter(jobs_df)
        if basic_filtered.empty:
            return basic_filtered
        
        # Traitement par batch
        return self._process_ai_batches(basic_filtered)
    
    def _basic_internship_filter(self, jobs_df: pd.DataFrame) -> pd.DataFrame:
        """Filtrage de base pour garder seulement les vrais stages - VERSION AMÉLIORÉE"""
        if jobs_df.empty:
            return jobs_df
            
        # ✅ MÊME LOGIQUE que filter_for_internships mais simplifiée pour l'IA
        stage_indicators = [
            'stage', 'stagiaire', 'intern', 'internship', 'étudiant', 'student',
            'fin d\'études', 'pfe', 'master', '6 mois', 'graduate', 'apprentice',
            'trainee', 'apprentissage', 'jeune diplômé', 'junior'
        ]
        
        # ✅ Exclusions intelligentes (pas de mots isolés)
        exclude_patterns = [
            'cdi temps plein', 'cdd temps plein', 'permanent position', 
            'senior manager', 'lead developer', 'chef d\'équipe expérimenté',
            'directeur', 'responsable équipe', '5+ années d\'expérience',
            'minimum 3 ans', 'expérience requise'
        ]
        
        def is_basic_internship_flexible(row) -> bool:
            title = str(row.get('title', '')).lower()
            description = str(row.get('description', '')).lower()
            text = f"{title} {description}"
            
            # 1. Doit être un stage
            has_stage = any(keyword in text for keyword in stage_indicators)
            if not has_stage:
                return False
            
            # 2. Ne doit pas avoir d'exclusions fortes
            has_exclude = any(pattern in text for pattern in exclude_patterns)
            if has_exclude:
                return False
                
            # 3. ✅ CRITÈRE DATA TRÈS SOUPLE pour l'IA
            data_indicators = [
                'data', 'python', 'analytics', 'statistique', 'analyse',
                'business intelligence', 'tableau', 'sql', 'informatique',
                'algorithme', 'reporting', 'dashboard', 'excel'
            ]
            
            has_data_hint = any(word in text for word in data_indicators)
            return has_data_hint  # ✅ Très permissif, l'IA fera le tri fin
        
        mask = jobs_df.apply(is_basic_internship_flexible, axis=1)
        filtered_df = jobs_df[mask].copy()
        
        print(f"🔍 Filtrage de base IA: {len(jobs_df)} → {len(filtered_df)} offres")
        return filtered_df
    def _process_ai_batches(self, filtered_df: pd.DataFrame, batch_size: int = 5) -> pd.DataFrame:
        """Traite les offres par batch avec l'IA"""
        filtered_jobs = []
        
        for i in range(0, len(filtered_df), batch_size):
            batch = filtered_df.iloc[i:i+batch_size]
            
            try:
                relevant_indices = self._analyze_batch_with_ai(batch)
                
                for idx in relevant_indices:
                    if idx < len(batch):
                        filtered_jobs.append(batch.iloc[idx])
                
                time.sleep(1)  # Rate limiting
                
            except Exception as e:
                logger.error(f"Erreur IA batch {i//batch_size + 1}: {e}")
                # Fallback sur filtrage classique
                fallback_filtered = self.filter_for_internships(batch)
                filtered_jobs.extend([row for _, row in fallback_filtered.iterrows()])
        
        if filtered_jobs:
            result_df = pd.DataFrame(filtered_jobs).reset_index(drop=True)
            logger.info(f"✅ {len(result_df)} stages DATA validés par IA")
            return result_df
        
        return pd.DataFrame()
    
    
    def _extract_missions_text(self, description: str) -> str:
        """
        Extrait la partie 'Missions' ou 'Description du poste' d'une offre.
        """
        if not isinstance(description, str):
            return ""
        if not description:
            return ""
        import re

        # Recherche des sections typiques
        patterns = [
            r"(?:missions?|principales missions?|objectifs?|description du poste|vos tâches|votre rôle)[\s:]*([\s\S]{0,1500})",
        ]

        for pattern in patterns:
            match = re.search(pattern, description, re.IGNORECASE)
            if match:
                # Retourne seulement la section Missions nettoyée
                text = match.group(1)
                text = re.split(r"\n[A-Z][^\n]{3,40}\n", text)[0]  # Coupe avant autre section (ex : Profil, Compétences)
                return text.strip()

        return description[:800]  # Fallback : les 800 premiers caractères
    def _analyze_batch_with_ai(self, batch_df: pd.DataFrame) -> List[int]:
        """Analyse un batch avec l'IA et retourne les indices pertinents"""
        jobs_for_analysis = []
        
        for idx, row in batch_df.iterrows():
            missions_text = self._extract_missions_text(row.get('description', ''))
            job_info = {
                "index": len(jobs_for_analysis),
                "title": str(row.get('title', '')),
                "company": str(row.get('company', '')),
                "location": str(row.get('location', '')),
                "missions": missions_text
}
            jobs_for_analysis.append(job_info)
        
        # ✅ Prompt simplifié et plus robuste
        system_prompt = """Tu es un recruteur expert en stages Data Science.

Analyse chaque offre et décide si elle correspond à un vrai stage en Data Science / Machine Learning / IA.

Pour chaque offre :
- Lis la section "missions" ou "description"
- Vérifie si les missions sont orientées vers l’analyse de données, apprentissage automatique, IA, BI, ou modélisation.
- Ignore les stages RH, marketing, finance, développement web pur, ou maintenance IT.

Réponds uniquement avec les indices des offres pertinentes (ex: "0,2,3") ou "aucun".
"""


        user_prompt = ""
        for job in jobs_for_analysis:
            user_prompt += f"[{job['index']}] {job['title']} - {job['company']}\n"
            user_prompt += f"Missions: {job['missions'][:500]}...\n\n"
                
        # ✅ Configuration API améliorée
        data = {
            "model": "openai/gpt-4o-mini",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "temperature": 0,  # ✅ Plus déterministe
            "max_tokens": 50   # ✅ Réponse courte
        }
        
        result = self._make_api_call(data)
        if not result:
            logger.warning("🤖 API IA échouée, fallback sur filtrage classique")
            return list(range(len(jobs_for_analysis)))  # ✅ Fallback: garder tout
        
        try:
            # ✅ Gestion robuste de la réponse
            content = result.get('choices', [{}])[0].get('message', {}).get('content')
            
            if not content:
                logger.warning("🤖 Réponse IA vide, fallback activé")
                return list(range(len(jobs_for_analysis)))
                
            ai_response = content.strip()
            logger.info(f"🤖 Réponse IA: '{ai_response}'")
            
            if ai_response.lower() in ["aucun", "none", ""]:
                return []
            
            # ✅ Parsing sécurisé
            try:
                indices = []
                for x in ai_response.split(','):
                    x = x.strip()
                    if x.isdigit():
                        idx = int(x)
                        if 0 <= idx < len(jobs_for_analysis):  # ✅ Vérification bounds
                            indices.append(idx)
                return indices
            except ValueError:
                logger.warning(f"🤖 Format réponse invalide: '{ai_response}', fallback activé")
                return list(range(len(jobs_for_analysis)))
                
        except Exception as e:
            logger.error(f"🤖 Erreur traitement réponse IA: {e}")
            return list(range(len(jobs_for_analysis)))  # ✅ Fallback sûr
    
    def save_results(self, all_jobs: List[pd.DataFrame], search_summary: List[Dict]) -> Optional[str]:
        """
        Sauvegarde les résultats dans Excel avec analyse et déduplication avancée
        """
        if not all_jobs:
            logger.error("❌ Aucun stage trouvé")
            return None
        
        # Combiner tous les résultats
        combined_df = pd.concat(all_jobs, ignore_index=True)
        logger.info(f"📊 {len(combined_df)} offres trouvées avant déduplication")
        
        # Déduplication avancée
        combined_df = self.remove_duplicates(combined_df)
        
        # Nom de fichier avec timestamp
        timestamp = datetime.now().strftime('%Y%m%d_%H%M')
        filename = f"stages_data_science_france_{timestamp}.xlsx"
        
        # Créer le fichier Excel avec plusieurs onglets
        with pd.ExcelWriter(filename, engine='openpyxl') as writer:
            # Onglet principal avec tous les stages
            combined_df.to_excel(writer, sheet_name='Tous_les_Stages', index=False)
            
            # Onglet par domaine
            for domain in self.domain_keywords.keys():
                domain_jobs = combined_df[combined_df['domaine_recherche'] == domain]
                if not domain_jobs.empty:
                    sheet_name = domain.replace('_', '_').title()[:31]  # Limite Excel
                    domain_jobs.to_excel(writer, sheet_name=sheet_name, index=False)
            
            # Onglet avec matching personnalisé si disponible
            if self.user_profile and 'match_score' in combined_df.columns:
                top_matches = combined_df.head(10)
                top_matches.to_excel(writer, sheet_name='Top_Matches', index=False)
            
            # Onglet résumé des recherches
            if search_summary:
                summary_df = pd.DataFrame(search_summary)
                summary_df.to_excel(writer, sheet_name='Résumé_Recherches', index=False)
        
        # Statistiques
        total_stages = len(combined_df)
        unique_companies = combined_df['company'].nunique() if 'company' in combined_df.columns else 0
        
        logger.info(f"\n📊 RÉSULTATS FINAUX:")
        logger.info(f"✅ {total_stages} stages uniques trouvés")
        logger.info(f"🏢 {unique_companies} entreprises uniques")
        logger.info(f"📁 Sauvegardé dans: {filename}")
        
        # Générer rapport personnalisé si applicable
        if self.user_profile and 'match_score' in combined_df.columns:
            self.generate_personalized_report(combined_df)
        
        # Top 5 des entreprises
        if 'company' in combined_df.columns and not combined_df.empty:
            top_companies = combined_df['company'].value_counts().head()
            logger.info(f"\n🏆 Top entreprises:")
            for company, count in top_companies.items():
                logger.info(f"   • {company}: {count} stages")
        
        return filename
    
    def generate_personalized_report(self, matched_jobs_df: pd.DataFrame) -> None:
        """Génère un rapport personnalisé des recommandations"""
        if matched_jobs_df.empty or not self.user_profile:
            return
        
        print("\n" + "="*60)
        print("🎯 RECOMMANDATIONS PERSONNALISÉES")
        print("="*60)
        
        print(f"👤 Profil: {self.user_profile.get('specialization', 'Data Science')}")
        print(f"💪 Forces: {', '.join(self.user_profile.get('key_strengths', [])[:3])}")
        print(f"🔧 Compétences: {', '.join(self.user_profile.get('technical_skills', [])[:5])}")
        
        if 'match_score' in matched_jobs_df.columns:
            top_matches = matched_jobs_df.head(5)
            
            print(f"\n🏆 TOP 5 STAGES RECOMMANDÉS:")
            print("-" * 40)
            
            for idx, (_, job) in enumerate(top_matches.iterrows(), 1):
                score = job.get('match_score', 0)
                reasons = job.get('match_reasons', [])
                
                print(f"\n{idx}. {job.get('title', 'N/A')}")
                print(f"   🏢 {job.get('company', 'N/A')}")
                print(f"   📍 {job.get('location', 'N/A')}")
                print(f"   🎯 Score: {score}/100")
                
                if reasons:
                    print(f"   ✅ Pourquoi: {' | '.join(reasons[:2])}")
                
                if job.get('job_url'):
                    print(f"   🔗 {job['job_url']}")
        
        print("\n" + "="*60)
    
    def run_complete_search(self) -> Optional[str]:
        """
        Lance une recherche complète et sauvegarde les résultats
        """
        start_time = datetime.now()
        
        try:
            # Afficher la configuration
            period_desc = self.publication_config.get(self.hours_old, f"{self.hours_old}h")
            logger.info(f"⚙️ Configuration: Période de {period_desc}")
            
            # Recherche
            all_jobs, search_summary = self.search_stages()
            
            # Sauvegarde
            filename = self.save_results(all_jobs, search_summary)
            
            # Temps total
            duration = datetime.now() - start_time
            logger.info(f"\n⏱️ Recherche terminée en {duration}")
            
            return filename
            
        except Exception as e:
            logger.error(f"❌ Erreur globale: {str(e)}")
            return None
    
    def _extract_cv_content(self, pdf_path: str) -> Optional[str]:
        """Extrait le contenu d'un CV PDF"""
        try:
            with open(pdf_path, 'rb') as file:
                pdf_reader = PyPDF2.PdfReader(file)
                content = ""
                for page in pdf_reader.pages:
                    content += page.extract_text() + "\n"
                return content.strip()
        except Exception as e:
            logger.error(f"Erreur lecture CV: {e}")
            return None
    
    def _analyze_cv_with_ai(self, cv_content: str) -> Optional[Dict]:
        """Analyse le CV avec l'IA"""
        if not self.openrouter_api_key:
            return None
        
        system_prompt = """Tu es un expert RH spécialisé en data science. Analyse ce CV et extrait les informations clés.

RÉPONDS STRICTEMENT en format JSON:
{
    "specialization": "Machine Learning" | "Data Science" | "Business Intelligence" | "Deep Learning" | "Computer Vision" | "NLP" | "Data Engineering" | "Généraliste Data Science",
    "technical_skills": ["Python", "R", "SQL", "PyTorch"],
    "domains": ["Finance", "Santé", "E-commerce"],
    "experience_level": "Débutant" | "Intermédiaire" | "Avancé",
    "key_strengths": ["Analyse statistique", "Machine Learning"]
}"""

        user_prompt = f"CONTENU DU CV:\n\n{cv_content[:2000]}"
        
        data = {
            "model": "openai/gpt-4o-mini",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "temperature": 0.1,
            "max_tokens": 500
        }
        
        result = self._make_api_call(data)
        if result:
            try:
                ai_response = result['choices'][0]['message']['content'].strip()
                return json.loads(ai_response)
            except json.JSONDecodeError as e:
                logger.error(f"Erreur parsing CV JSON: {e}")
        
        return None
    
    def ai_match_with_profile(self, jobs_df: pd.DataFrame) -> pd.DataFrame:
        """Matching intelligent avec le profil utilisateur"""
        if jobs_df.empty or not self.user_profile:
            return self.ai_filter_internships(jobs_df)
        
        logger.info(f"🎯 Matching personnalisé pour {self.user_profile.get('specialization', 'Data Science')}")
        
        basic_filtered = self._basic_internship_filter(jobs_df)
        if basic_filtered.empty:
            return basic_filtered
        
        return self._process_profile_matching(basic_filtered)
    
    def _process_profile_matching(self, filtered_df: pd.DataFrame, batch_size: int = 3) -> pd.DataFrame:
        """Traite le matching par profil par batch"""
        matched_jobs = []
        
        for i in range(0, len(filtered_df), batch_size):
            batch = filtered_df.iloc[i:i+batch_size]
            
            try:
                matches = self._match_batch_with_profile(batch)
                
                for match in matches:
                    idx = match['index']
                    if idx < len(batch):
                        job_row = batch.iloc[idx].copy()
                        job_row['match_score'] = match['score']
                        job_row['match_reasons'] = match['reasons']
                        matched_jobs.append(job_row)
                
                time.sleep(1.5)
                
            except Exception as e:
                logger.error(f"Erreur matching batch: {e}")
                fallback = self.ai_filter_internships(batch)
                matched_jobs.extend([row for _, row in fallback.iterrows()])
        
        if matched_jobs:
            result_df = pd.DataFrame(matched_jobs).reset_index(drop=True)
            if 'match_score' in result_df.columns:
                result_df = result_df.sort_values('match_score', ascending=False)
            logger.info(f"✅ {len(result_df)} stages matchés avec votre profil")
            return result_df
        
        return pd.DataFrame()
    
    def _match_batch_with_profile(self, batch_df: pd.DataFrame) -> List[Dict]:
        """Matching d'un batch avec le profil utilisateur"""
        jobs_for_analysis = []
        
        for idx, row in batch_df.iterrows():
            missions_text = self._extract_missions_text(row.get('description', ''))
            job_info = {
                "index": len(jobs_for_analysis),
                "title": str(row.get('title', '')),
                "company": str(row.get('company', '')),
                "location": str(row.get('location', '')),
                "missions": missions_text
}
            jobs_for_analysis.append(job_info)
        
        # Prompt de matching personnalisé
        system_prompt = f"""Tu es un consultant carrière spécialisé en IA/Data Science.

PROFIL CANDIDAT:
- Spécialisation: {self.user_profile.get('specialization', 'Data Science')}
- Compétences: {', '.join(self.user_profile.get('technical_skills', []))}
- Domaines: {', '.join(self.user_profile.get('domains', []))}
- Forces: {', '.join(self.user_profile.get('key_strengths', []))}

Pour chaque offre :
1. Lis la section "missions".
2. Évalue la correspondance entre ces missions et le profil du candidat.
3. Donne un score (0 à 100) et 2 raisons courtes.

Réponds en JSON strict:
[
  {{"index": 0, "score": 85, "reasons": ["Stage Data Scientist", "Python & ML utilisés"]}},
  {{"index": 1, "score": 55, "reasons": ["Analyse de données, peu de ML"]}}
]
"""

        user_prompt = ""
        for job in jobs_for_analysis:
            user_prompt += f"[{job['index']}] {job['title']}\n"
            user_prompt += f"Entreprise: {job['company']}\n"
            user_prompt += f"Lieu: {job['location']}\n"
            user_prompt += f"Missions: {job['missions']}\n\n"

        
        data = {
            "model": "openai/gpt-4o-mini",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "temperature": 0.2,
            "max_tokens": 600
        }
        
        result = self._make_api_call(data)
        if result:
            try:
                ai_response = result['choices'][0]['message']['content'].strip()
                matches = json.loads(ai_response)
                return [match for match in matches if match.get('score', 0) >= 50]
            except json.JSONDecodeError as e:
                logger.error(f"Erreur parsing matching JSON: {e}")
        
        return []
    
    def remove_duplicates(self, df: pd.DataFrame) -> pd.DataFrame:
        """Suppression avancée des doublons"""
        if df.empty:
            return df
        
        original_count = len(df)
        logger.info("🔍 Déduplication en cours...")
        
        # Nettoyer les URLs
        if 'job_url' in df.columns:
            df['job_url_clean'] = df['job_url'].apply(self._clean_url)
            # Supprimer doublons d'URL
            df_dedup = df.drop_duplicates(subset=['job_url_clean'], keep='first')
            url_dups = original_count - len(df_dedup)
            if url_dups > 0:
                logger.info(f"   🗑️ {url_dups} doublons d'URL supprimés")
        else:
            df_dedup = df.copy()
        
        # Créer clé de déduplication sémantique
        df_dedup['dedup_key'] = df_dedup.apply(self._create_dedup_key, axis=1)
        df_final = df_dedup.drop_duplicates(subset=['dedup_key'], keep='first')
        
        semantic_dups = len(df_dedup) - len(df_final)
        if semantic_dups > 0:
            logger.info(f"   🗑️ {semantic_dups} doublons sémantiques supprimés")
        
        # Nettoyer colonnes temporaires
        df_final = df_final.drop(['job_url_clean', 'dedup_key'], axis=1, errors='ignore')
        
        total_removed = original_count - len(df_final)
        logger.info(f"✅ {len(df_final)} offres uniques ({total_removed} doublons supprimés)")
        
        return df_final
    
    def _clean_url(self, url: str) -> str:
        """Nettoie une URL en supprimant les paramètres de tracking"""
        if pd.isna(url) or not url:
            return ""
        
        try:
            tracking_params = [
                'utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term',
                'ref', 'source', 'campaign_id', 'click_id', 'gclid', 'fbclid'
            ]
            
            if '?' in url:
                base_url, params = url.split('?', 1)
                param_pairs = params.split('&')
                clean_params = []
                
                for param in param_pairs:
                    if '=' in param:
                        key, _ = param.split('=', 1)
                        if key.lower() not in tracking_params:
                            clean_params.append(param)
                
                if clean_params:
                    return f"{base_url}?{'&'.join(clean_params)}"
                else:
                    return base_url
            else:
                return url.rstrip('/')
        except Exception:
            return url
    
    def _create_dedup_key(self, row) -> str:
        """Crée une clé de déduplication basée sur titre, entreprise et localisation"""
        def normalize_text(text):
            if pd.isna(text) or not text:
                return ""
            import re
            normalized = re.sub(r'[^\w\s]', ' ', str(text).lower())
            normalized = re.sub(r'\s+', ' ', normalized).strip()
            return normalized
        
        title = normalize_text(row.get('title', ''))
        company = normalize_text(row.get('company', ''))
        location = normalize_text(row.get('location', ''))
        
        return f"{title}|{company}|{location}"

    

# Fonctions utilitaires pour créer des recherches personnalisées
def create_custom_search(hours_old: Optional[int] = None, max_results_per_city: int = 15, 
                        search_delay: int = 2, use_ai_filtering: bool = False) -> Optional[str]:
    """
    Fonction utilitaire pour créer une recherche personnalisée
    
    Args:
        hours_old: Période en heures (24, 72, 168, 336, 720, None)
        max_results_per_city: Nombre max de résultats par ville/mot-clé
        search_delay: Délai en secondes entre les recherches
        use_ai_filtering: Utiliser le filtrage IA intelligent
    
    Returns:
        str: Nom du fichier Excel généré
    """
    # Créer l'instance avec configuration personnalisée
    finder = StageFinder(
        hours_old=hours_old, 
        use_ai_filtering=use_ai_filtering,
        openrouter_api_key=OPENROUTER_API_KEY if use_ai_filtering else None,
        cv_path=CV_PATH if use_ai_filtering else None
    )
    
    # Configuration des paramètres de recherche
    finder.max_results = max_results_per_city
    finder.delay = search_delay
    
    # Lancer la recherche
    return finder.run_complete_search()


# Fonctions avec support IA
def search_with_ai(hours_old: int = 168) -> Optional[str]:
    """Recherche avec filtrage IA intelligent et matching CV"""
    if CV_PATH:
        print("🎯 RECHERCHE AVEC IA + MATCHING CV PERSONNALISÉ")
    else:
        print("🤖 RECHERCHE AVEC IA (Filtrage Intelligent)")
    return create_custom_search(
        hours_old=hours_old,
        max_results_per_city=25,  # Plus de résultats car l'IA filtre mieux
        use_ai_filtering=True
    )

def search_last_day_ai() -> Optional[str]:
    """Recherche quotidienne avec IA"""
    print("🤖 IA - RECHERCHE QUOTIDIENNE (24h)")
    return search_with_ai(hours_old=24)

def search_last_week_ai() -> Optional[str]:
    """Recherche hebdomadaire avec IA"""
    print("🤖 IA - RECHERCHE HEBDOMADAIRE (7 jours)")
    return search_with_ai(hours_old=168)

def search_last_month_ai() -> Optional[str]:
    """Recherche mensuelle avec IA"""
    print("🤖 IA - RECHERCHE MENSUELLE (30 jours)")
    return search_with_ai(hours_old=720)


# Configurations prédéfinies sans IA
def search_last_24h() -> Optional[str]:
    """Recherche stages publiés dans les dernières 24h"""
    print("🚀 RECHERCHE ULTRA-RÉCENTE (24h)")
    return create_custom_search(hours_old=24, max_results_per_city=10)

def search_last_week() -> Optional[str]:
    """Recherche stages publiés dans la dernière semaine"""
    print("📅 RECHERCHE HEBDOMADAIRE (7 jours)")
    return create_custom_search(hours_old=168, max_results_per_city=20)

def search_last_month() -> Optional[str]:
    """Recherche stages publiés dans le dernier mois"""
    print("📆 RECHERCHE MENSUELLE (30 jours)")
    return create_custom_search(hours_old=720, max_results_per_city=25)

def search_all_time() -> Optional[str]:
    """Recherche tous les stages disponibles"""
    print("🌍 RECHERCHE COMPLÈTE (toutes périodes)")
    return create_custom_search(hours_old=None, max_results_per_city=30)


# Installation automatique des dépendances
def install_requirements() -> bool:
    """Installe les dépendances requises"""
    required_packages = [
        'python-jobspy',
        'pandas', 
        'openpyxl',
        'PyPDF2',
        'requests'
    ]
    
    print("📦 Vérification des dépendances...")
    missing_packages = []
    
    for package in required_packages:
        try:
            __import__(package.replace('-', '_'))
        except ImportError:
            missing_packages.append(package)
    
    if missing_packages:
        print(f"⚠️ Packages manquants: {', '.join(missing_packages)}")
        print("💾 Installer avec: pip install " + " ".join(missing_packages))
        return False
    else:
        print("✅ Toutes les dépendances sont installées")
        return True
def test_api():
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json"
        }
        
        test_data = {
            "model": "openai/gpt-4o-mini",  # ✅ Modèle corrigé
            "messages": [
                {"role": "user", "content": "Réponds juste '1,2' pour tester"}
            ],
            "max_tokens": 50
        }
        
        try:
            response = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers,
                json=test_data,
                timeout=15
            )
            print(f"Status: {response.status_code}")
            print(f"Response: {response.json()}")
            return response.status_code == 200
        except Exception as e:
            print(f"Erreur API: {e}")
            return False

# Main execution
if __name__ == "__main__":
    print("🎓 RECHERCHE DE STAGES DATA SCIENCE - FRANCE")
    print("=" * 50)

    # Vérifier les dépendances
    # if not install_requirements():
    #     print("\n❌ Veuillez installer les dépendances manquantes avant de continuer.")
    #     exit(1)

    # ✅ Test API OpenRouter
    print("\n🧪 Test de l'API...")
    if test_api():
        print("✅ API fonctionnelle !")
    else:
        print("❌ Problème API détecté")
        exit(1)

    # Choix de configuration
    print("\n🔧 CONFIGURATIONS DISPONIBLES:")
    print("1. Dernières 24h (ultra-récent)")
    print("2. 3 derniers jours (récent)")
    print("3. Dernière semaine (recommandé)")
    print("4. 2 dernières semaines (étendu)")
    print("5. Dernier mois (complet)")
    print("6. Toutes périodes (exhaustif)")
    print("7. 🤖 IA - Dernier jour")
    print("8. 🤖 Recherche avec IA (recommandé)")
    print("9. 🤖 IA - Dernière 3 jours ")
    print("10. 🤖 IA - Dernière semaine")
    print("11. 🤖 IA - Dernier mois")
    print("12. Configuration personnalisée")

    # Afficher l'état de l'IA
    if OPENROUTER_API_KEY:
        print(f"\n✅ IA activée avec OpenRouter")
        if CV_PATH and os.path.exists(CV_PATH):
            print(f"📄 CV détecté: {CV_PATH}")
        else:
            print("📄 Pas de CV configuré (recherche IA générique)")
    else:
        print("\n❌ IA désactivée (pas de clé API)")

    # ✅ Nouvelle question pour JobTeaser
    include_jobteaser = input("\nVoulez-vous inclure JobTeaser dans la recherche ? (O/n): ").strip().lower() != 'n'

    try:
        choice = input(f"\nChoisissez une option (1-11) ou Entrée pour option {'8' if OPENROUTER_API_KEY else '3'}: ").strip()

        # ✅ On transmet include_jobteaser à StageFinder via une instance dédiée
        def run_with_jobteaser(func):
            finder = StageFinder(
                hours_old=None,
                use_ai_filtering=(OPENROUTER_API_KEY is not None),
                openrouter_api_key=OPENROUTER_API_KEY if OPENROUTER_API_KEY else None,
                cv_path=CV_PATH if OPENROUTER_API_KEY else None
            )
            finder.enable_jobteaser = include_jobteaser
            return func(finder)

        if choice == "1":
            finder = StageFinder(hours_old=24)
            finder.enable_jobteaser = include_jobteaser
            result_file = finder.run_complete_search()
        elif choice == "2":
            finder = StageFinder(hours_old=72)
            finder.enable_jobteaser = include_jobteaser
            result_file = finder.run_complete_search()
        elif choice == "3":
            finder = StageFinder(hours_old=168)
            finder.enable_jobteaser = include_jobteaser
            result_file = finder.run_complete_search()
        elif choice == "4":
            finder = StageFinder(hours_old=336)
            finder.enable_jobteaser = include_jobteaser
            result_file = finder.run_complete_search()
        elif choice == "5":
            finder = StageFinder(hours_old=720)
            finder.enable_jobteaser = include_jobteaser
            result_file = finder.run_complete_search()
        elif choice == "6":
            finder = StageFinder(hours_old=None)
            finder.enable_jobteaser = include_jobteaser
            result_file = finder.run_complete_search()
        elif choice == "7":
            if OPENROUTER_API_KEY:
                finder = StageFinder(hours_old=24, use_ai_filtering=True, openrouter_api_key=OPENROUTER_API_KEY, cv_path=CV_PATH)
                finder.enable_jobteaser = include_jobteaser
                result_file = finder.run_complete_search()
            else:
                print("❌ Clé API requise pour l'IA, utilisation du mode classique")
                finder = StageFinder(hours_old=24)
                finder.enable_jobteaser = include_jobteaser
                result_file = finder.run_complete_search()
        elif choice == "8":
            if OPENROUTER_API_KEY:
                finder = StageFinder(hours_old=168, use_ai_filtering=True, openrouter_api_key=OPENROUTER_API_KEY, cv_path=CV_PATH)
                finder.enable_jobteaser = include_jobteaser
                result_file = finder.run_complete_search()
            else:
                print("❌ Clé API requise pour l'IA, utilisation du mode classique")
                finder = StageFinder(hours_old=168)
                finder.enable_jobteaser = include_jobteaser
                result_file = finder.run_complete_search()

        elif choice == "9":
            if OPENROUTER_API_KEY:
                finder = StageFinder(hours_old=72, use_ai_filtering=True, openrouter_api_key=OPENROUTER_API_KEY, cv_path=CV_PATH)
                finder.enable_jobteaser = include_jobteaser
                result_file = finder.run_complete_search()
            else:
                print("❌ Clé API requise pour l'IA, utilisation du mode classique")
                finder = StageFinder(hours_old=72)
                finder.enable_jobteaser = include_jobteaser
                result_file = finder.run_complete_search()
        
        elif choice == "10":
            if OPENROUTER_API_KEY:
                finder = StageFinder(hours_old=168, use_ai_filtering=True, openrouter_api_key=OPENROUTER_API_KEY, cv_path=CV_PATH)
                finder.enable_jobteaser = include_jobteaser
                result_file = finder.run_complete_search()
            else:
                print("❌ Clé API requise pour l'IA, utilisation du mode classique")
                finder = StageFinder(hours_old=168)
                finder.enable_jobteaser = include_jobteaser
                result_file = finder.run_complete_search()
        elif choice == "11":
            if OPENROUTER_API_KEY:
                finder = StageFinder(hours_old=720, use_ai_filtering=True, openrouter_api_key=OPENROUTER_API_KEY, cv_path=CV_PATH)
                finder.enable_jobteaser = include_jobteaser
                result_file = finder.run_complete_search()
            else:
                print("❌ Clé API requise pour l'IA, utilisation du mode classique")
                finder = StageFinder(hours_old=720)
                finder.enable_jobteaser = include_jobteaser
                result_file = finder.run_complete_search()
        elif choice == "12":
            print("\n⚙️ Configuration personnalisée:")
            hours = input("Nombre d'heures (ou 'all' pour tout): ").strip()
            if hours.lower() == 'all':
                hours_val = None
            else:
                hours_val = int(hours) if hours.isdigit() else 168

            max_results = input("Résultats max par recherche (défaut: 20): ").strip()
            max_results_val = int(max_results) if max_results.isdigit() else 20

            if OPENROUTER_API_KEY:
                use_ai = input("Utiliser filtrage IA? (O/n): ").strip().lower() != 'n'
            else:
                use_ai = False
                print("IA non disponible, utilisation du filtrage classique")

            finder = StageFinder(hours_old=hours_val, use_ai_filtering=use_ai, openrouter_api_key=OPENROUTER_API_KEY if use_ai else None, cv_path=CV_PATH if use_ai else None)
            finder.enable_jobteaser = include_jobteaser
            finder.max_results = max_results_val
            result_file = finder.run_complete_search()
        else:
            finder = StageFinder(hours_old=168, use_ai_filtering=(OPENROUTER_API_KEY is not None), openrouter_api_key=OPENROUTER_API_KEY, cv_path=CV_PATH)
            finder.enable_jobteaser = include_jobteaser
            result_file = finder.run_complete_search()

    except KeyboardInterrupt:
        print("\n❌ Recherche interrompue par l'utilisateur")
        result_file = None
    except Exception as e:
        print(f"❌ Erreur: {e}")
        print("🔄 Lancement avec configuration par défaut...")
        finder = StageFinder(hours_old=168)
        finder.enable_jobteaser = include_jobteaser
        result_file = finder.run_complete_search()

    # Résultat final
    if result_file:
        print(f"\n🎉 Recherche terminée avec succès!")
        print(f"📂 Ouvrez le fichier: {result_file}")
        print("\n💡 Conseils:")
        print("   • Vérifiez l'onglet 'Tous_les_Stages' pour une vue d'ensemble")
        print("   • Explorez les onglets par domaine pour des recherches ciblées")
        print("   • Consultez 'Résumé_Recherches' pour les statistiques")
        if CV_PATH and OPENROUTER_API_KEY:
            print("   • 'Top_Matches' pour vos meilleures compatibilités")
    else:
        print("\n❌ Échec de la recherche. Vérifiez votre connexion internet.")

    print("\n🚀 RECHERCHES RAPIDES DISPONIBLES:")
    print("   search_last_24h()           # Stages très récents")
    print("   search_last_week()          # Recommandé pour usage général")
    print("   search_last_month()         # Vue d'ensemble complète")
    print("   search_all_time()           # Recherche exhaustive")

    if OPENROUTER_API_KEY:
        print("\n🤖 RECHERCHES AVEC IA:")
        print("   search_last_day_ai()        # IA quotidien")
        print("   search_with_ai()            # IA intelligent")
        print("   search_last_week_ai()       # IA + 1 semaine")
        print("   search_last_month_ai()      # IA + 1 mois")
