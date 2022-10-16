import logging
import time
import os
import random
from pathlib import Path
from typing import Any, Dict, List
import concurrent.futures
import multiprocessing
from itertools import combinations


import joblib
import numpy as np
from sklearn.cluster import KMeans, MiniBatchKMeans
from scipy.stats import wasserstein_distance
from tqdm import tqdm
from poker_ai.clustering.card_combos import CardCombos, CardCombosAbstract
from poker_ai.clustering.game_utility import GameUtility, GameUtilityAbstract
from poker_ai.clustering.preflop import compute_preflop_lossless_abstraction
from poker_ai.poker.evaluation import Evaluator

from poker_ai.poker.card import Card

from poker_ai.poker.deck import get_all_suits


log = logging.getLogger("poker_ai.clustering.runner")

class CardInfoLutBuilder():
    '''Initializes a CardInfoLutProcessor and CardInfoLutStore'''


    pass

def main(n_simulations_river: int,
        n_simulations_turn: int,
        n_simulations_flop: int,
        low_card_rank: int,
        high_card_rank: int,
        save_dir: str,):
    processor = CardInfoLutProcessor(n_simulations_river=n_simulations_river,
        n_simulations_turn=n_simulations_turn,
        n_simulations_flop=n_simulations_flop,
        low_card_rank=low_card_rank,
        high_card_rank=high_card_rank,
        save_dir=save_dir)
    storage = CardInfoLutStore(low_card_rank=low_card_rank,
        high_card_rank=high_card_rank,
        save_dir=save_dir)

    storage.card_info = {}
    storage.centroids = {}
    river = storage.get_unique_combos(5)

     
    storage.card_info['river'], storage.centroids['river']  = processor.compute_river(river, 5)

    return storage


class CardInfoLutProcessor():
    '''Contains all the main methods to perform processing.'''
    def __init__(
        self,
        n_simulations_river: int,
        n_simulations_turn: int,
        n_simulations_flop: int,
        low_card_rank: int,
        high_card_rank: int,
        save_dir: str,
    ):
        self.n_simulations_river = n_simulations_river
        self.n_simulations_turn = n_simulations_turn
        self.n_simulations_flop = n_simulations_flop
        self.low_card_rank = low_card_rank
        self.high_card_rank = high_card_rank
        self.save_dir = save_dir
        self._evaluator = Evaluator()

        suits: List[str] = sorted(list(get_all_suits()))
        ranks: List[int] = sorted(list(range(low_card_rank, high_card_rank + 1)))
        self._cards = [int(Card(rank, suit)) for suit in suits for rank in ranks]
        self._cards.sort(reverse=True)


    def compute(
        self, n_river_clusters: int, n_turn_clusters: int, n_flop_clusters: int,
    ):
        """Compute all clusters and save to card_info_lut dictionary.

        Will attempt to load previous progress and will save after each cluster
        is computed.
        """
        log.info("Starting computation of clusters.")
        start = time.time()
        if "pre_flop" not in self.card_info_lut:
            self.card_info_lut["pre_flop"] = compute_preflop_lossless_abstraction(
                builder=self
            )
            joblib.dump(self.card_info_lut, self.card_info_lut_path)
        if "river" not in self.card_info_lut:
            self.card_info_lut["river"] = self._compute_river_clusters(
                n_river_clusters,
            )
            joblib.dump(self.card_info_lut, self.card_info_lut_path)
            joblib.dump(self.centroids, self.centroid_path)
        if "turn" not in self.card_info_lut:
            self.card_info_lut["turn"] = self._compute_turn_clusters(n_turn_clusters)
            joblib.dump(self.card_info_lut, self.card_info_lut_path)
            joblib.dump(self.centroids, self.centroid_path)
        if "flop" not in self.card_info_lut:
            self.card_info_lut["flop"] = self._compute_flop_clusters(n_flop_clusters)
            joblib.dump(self.card_info_lut, self.card_info_lut_path)
            joblib.dump(self.centroids, self.centroid_path)
        end = time.time()
        log.info(f"Finished computation of clusters - took {end - start} seconds.")

    def compute_river(self, river, n_river_clusters: int):
        """Compute river clusters and create lookup table."""
        log.info("Starting computation of river clusters.")
        start = time.time()

        # Here is computing the expected hand strength of each.
        with concurrent.futures.ProcessPoolExecutor(os.cpu_count()) as executor:
            river_ehs = list(tqdm(
                    executor.map(
                        self.process_river_ehs,
                        river,
                        chunksize=len(river) // os.cpu_count(),
                    ),
                    total=len(river)))

            
        end = time.time()
        log.info(
            f"Finished computation of river HS - took {end - start} seconds."
        )

        river_ehs = np.array(river_ehs).reshape(-1, 3)
        # Here then cluster based on hand strengths.
        centroids, clusters = self.cluster(
            num_clusters=n_river_clusters, X=river_ehs
        )
        end = time.time()
        log.info(
            f"Finished computation and clustering of river - took {end - start} seconds."
        )

        return self.create_card_lookup(clusters, river), centroids

    def _compute_turn_clusters(self, turn, n_turn_clusters: int):
        """Compute turn clusters and create lookup table."""
        log.info("Starting computation of turn clusters.")
        start = time.time()

        with concurrent.futures.ProcessPoolExecutor(os.cpu_count()) as executor:
            self._turn_ehs_distributions = list(
                tqdm(
                    executor.map(
                        self.process_turn_ehs_distributions,
                        turn,
                        chunksize=len(turn) // 64,
                        # chunksize=64000,
                    ),
                    total=len(turn),
                )
            )

        end = time.time()
        log.info(
            f"Finished computation of turn HS - took {end - start} seconds."
        )

        self.centroids["turn"], self._turn_clusters = self.cluster(
            num_clusters=n_turn_clusters, X=self._turn_ehs_distributions
        )
        end = time.time()
        log.info(f"Finished computation and clustering of turn - took {end - start} seconds.")
        return None
        return self.create_card_lookup(self._turn_clusters, self.turn)

    def _compute_flop_clusters(self, n_flop_clusters: int):
        """Compute flop clusters and create lookup table."""
        log.info("Starting computation of flop clusters.")
        start = time.time()
        with concurrent.futures.ProcessPoolExecutor(os.cpu_count()-1) as executor:
            self._flop_potential_aware_distributions = list(
                tqdm(
                    executor.map(
                        self.process_flop_potential_aware_distributions,
                        self.flop,
                        chunksize=len(self.flop) // 64,
                    ),
                    total=len(self.flop),
                )
            )
        self.centroids["flop"], self._flop_clusters = self.cluster(
            num_clusters=n_flop_clusters, X=self._flop_potential_aware_distributions
        )
        end = time.time()
        log.info(f"Finished computation of flop clusters - took {end - start} seconds.")
        return self.create_card_lookup(self._flop_clusters, self.flop)

    def simulate_get_ehs(self, game: GameUtilityAbstract,) -> np.ndarray:
        """
        Get expected hand strength object.

        Parameters
        ----------
        game : GameUtility
            GameState for help with determining winner and sampling opponent hand

        Returns
        -------
        ehs : np.ndarray
            [win_rate, loss_rate, tie_rate]
        """
        ehs: np.ndarray = np.zeros(3)
        for _ in range(self.n_simulations_river):
            idx: int = game.get_winner()
            # increment win rate for winner/tie
            ehs[idx] += 1 / self.n_simulations_river
        return ehs

    def simulate_get_turn_ehs_distributions(
        self,
        available_cards: list,
        board: list,
        our_hand: list,
    ) -> np.ndarray:
        """
        Get histogram of frequencies that a given turn situation resulted in a
        certain cluster id after a river simulation.

        Parameters
        ----------
        available_cards : np.ndarray
            Array of available cards on the turn
        board : np.nearray
            The board as of the turn
        our_hand : np.ndarray
            Cards our hand (Card)

        Returns
        -------
        turn_ehs_distribution : np.ndarray
            Array of counts for each cluster the turn fell into by the river
            after simulations
        """
        turn_ehs_distribution = np.zeros(len(self.centroids["river"]))
        # sample river cards and run a simulation
        for _ in range(self.n_simulations_turn):
            river_card = random.choice(available_cards)
            # Temporary board.
            the_board = board + [river_card]

            game = GameUtilityAbstract(our_hand=our_hand, board=the_board, 
                    cards=self._cards, evaluator=self._evaluator)
            ehs = self.simulate_get_ehs(game)

            dist = np.linalg.norm(ehs - self.centroids["river"], axis =1)
            min_idx = dist.argmin()

            # now increment the cluster to which it belongs -
            turn_ehs_distribution[min_idx] += 1 / self.n_simulations_turn
        return turn_ehs_distribution

    def process_river_ehs(self, thing) -> np.ndarray:
        """
        Get the expected hand strength for a particular card combo.

        Parameters
        ----------
        public : np.ndarray
            Cards to process

        Returns
        -------
            Expected hand strength
        """
        res = []
        our_hand, public = thing
        for board in public:

            # Get expected hand strength
            game = GameUtilityAbstract(our_hand=our_hand, 
            board=board, cards=self._cards, evaluator=self._evaluator)
            res.append(self.simulate_get_ehs(game))
        return res

    @staticmethod
    def get_available_cards(
        cards: np.ndarray, unavailable_cards: np.ndarray
    ) -> np.ndarray:
        """
        Get all cards that are available.

        Parameters
        ----------
        cards : np.ndarray
        unavailable_cards : np.array
            Cards that are not available.

        Returns
        -------
            Available cards
        """
        # Turn into set for O(1) lookup speed.
        unavailable_cards = set(unavailable_cards)
        return [c for c in cards if c not in unavailable_cards]

    def process_turn_ehs_distributions(self, public: np.ndarray) -> np.ndarray:
        """
        Get the potential aware turn distribution for a particular card combo.

        Parameters
        ----------
        public : np.ndarray
            Cards to process

        Returns
        -------
            Potential aware turn distributions
        """

        available_cards = self.get_available_cards(
            cards=self._cards, unavailable_cards=public
        )
        # sample river cards and run a simulation
        turn_ehs_distribution = self.simulate_get_turn_ehs_distributions(
            available_cards, board=public[2:6], our_hand=public[:2],
        )
        return turn_ehs_distribution

    def process_flop_potential_aware_distributions(
        self, public: list,
    ) -> np.ndarray:
        """
        Get the potential aware flop distribution for a particular card combo.

        Parameters
        ----------
        public : np.ndarray
            Cards to process

        Returns
        -------
            Potential aware flop distributions
        """
        available_cards = self.get_available_cards(
            cards=self._cards, unavailable_cards=public
        )
        potential_aware_distribution_flop = np.zeros(len(self.centroids["turn"]))
        for j in range(self.n_simulations_flop):
            # randomly generating turn
            turn_card = random.choice(available_cards)
            our_hand = public[:2]
            board = public[2:5]
            the_board = board + [turn_card]
            # getting available cards
            available_cards_turn = [x for x in available_cards if x != turn_card]

            # This line is around 80% of compute time
            turn_ehs_distribution = self.simulate_get_turn_ehs_distributions(
                available_cards_turn, board=the_board, our_hand=our_hand,
            )
            
            # This part is around 20% of compute time
            dist = np.linalg.norm(turn_ehs_distribution - self.centroids["turn"], axis =1)
            min_idx = dist.argmin()

            # Now increment the cluster to which it belongs.
            potential_aware_distribution_flop[min_idx] += 1 / self.n_simulations_flop
        return potential_aware_distribution_flop

    @staticmethod
    def cluster(num_clusters: int, X: np.ndarray):
        # km = KMeans(
        #     n_clusters=num_clusters,
        #     init="random",
        #     n_init=10,
        #     max_iter=300,
        #     tol=1e-04,
        #     random_state=0,
        # )
        km = MiniBatchKMeans(
            n_clusters=num_clusters,
            init="k-means++",
            batch_size=5096,
            n_init=10,
            max_iter=300,
            tol=1e-04,
            random_state=0,
        )
        
        y_km = km.fit_predict(X)
        # Centers to be used for r - 1 (ie; the previous round)
        centroids = km.cluster_centers_
        return centroids, y_km

    @staticmethod
    def create_card_lookup(clusters: np.ndarray, card_combos: np.ndarray) -> Dict:
        """
        Create lookup table.

        Parameters
        ----------
        clusters : np.ndarray
            Array of cluster ids.
        card_combos : np.ndarray
            The card combos to which the cluster ids belong.

        Returns
        -------
        lossy_lookup : Dict
            Lookup table for finding cluster ids.
        """
        log.info("Creating lookup table.")
        lossy_lookup = {}
        for i, (starthand,valids) in enumerate(tqdm(card_combos)):
            for valid in valids:
                lossy_lookup[tuple(starthand+valid)] = clusters[i]
        return lossy_lookup


class CardInfoLutStore():
    """
    Stores info buckets for each street.
    Stores all shit that needs to be stored.

    Attributes
    ----------
    card_info_lut : Dict[str, Any]
        Lookup table of card combinations per betting round to a cluster id.
    centroids : Dict[str, Any]
        Centroids per betting round for use in clustering previous rounds by
        earth movers distance.
    """

    def __init__(
        self,
        low_card_rank: int,
        high_card_rank: int,
        save_dir: str,
    ):
        self.card_info_lut_path: Path = Path(save_dir) / "card_info_lut.joblib"
        self.centroid_path: Path = Path(save_dir) / "centroids.joblib"
        try:
            self.card_info_lut: Dict[str, Any] = joblib.load(self.card_info_lut_path)
            self.centroids: Dict[str, Any] = joblib.load(self.centroid_path)
        except FileNotFoundError:
            self.centroids: Dict[str, Any] = {}
            self.card_info_lut: Dict[str, Any] = {}

        suits: List[str] = sorted(list(get_all_suits()))
        ranks: List[int] = sorted(list(range(low_card_rank, high_card_rank + 1)))
        self.cards = [int(Card(rank, suit)) for suit in suits for rank in ranks]
        self.cards.sort(reverse=True)
        self.starting_hands = self.get_card_combos(2, self.cards)

    def get_card_combos(self, num_cards: int, deck: list) -> list:
        """
        Get the card combinations for a given street.

        Parameters
        ----------
        num_cards : int
            Number of cards you want returned

        Returns
        -------
            Combos of cards (Card) -> np.ndarray
        """
        res = [sorted(c, reverse=True) for c in combinations(deck, num_cards)]
        res.sort(reverse=True)
        res = [tuple(c) for c in res]
        return res

    def get_unique_combos(self, n_public: int) ->list:
        """
        Get the card combinations for a given street.
        Will return an iterator of unique combinations.

        Parameters
        ----------
        starting_hands : list
            Starting hands that you have, should be a list of 2 ints.
        n_public : int
            How many public cards.

        Returns
        -------
            iterator of unique combos
        """
        valid_cards = []
        for starting_hand in self.starting_hands:
            valid_card = [c for c in self.cards if c not in starting_hand]
            valid_cards.append(valid_card)

        res = [(start, combinations(valid, n_public)) for start, valid in zip(self.starting_hands, valid_cards)]
        
        return res